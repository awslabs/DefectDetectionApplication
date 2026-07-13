"""
Unit tests for Component_Packager packaging atomicity
(functions/workflow_packaging.py).

Task 7.2 (spec: workflow-manager). Simulated artifact upload failures
assert stage cleanup, failing-artifact reporting, and the absence of
partial component versions: on any packaging failure the staging and
promoted prefixes are deleted, the failing artifact is identified in a
502 PACKAGING_FAILED response, and no Greengrass component version is
registered (a version that fails to become DEPLOYABLE is deleted).
_Requirements: 7.5_
"""
import io
import json
import sys
import uuid
import zipfile
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

STAGING_ROOT = "workflows/staging"
COMPONENTS_ROOT = "workflows/components"
PLUGIN_PREFIX = "workflow-plugins"


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging(aws_stack):
    """Import workflow_packaging inside the moto mock so its module-level
    boto3 clients (portal DynamoDB / S3) are intercepted."""
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


class FailingClientProxy:
    """Wraps a real (moto-backed) boto3 client, raising ClientError from
    one named operation when the request Key matches, to simulate an
    artifact upload / promote failure."""

    def __init__(self, real_client, fail_op, key_contains=""):
        self._real = real_client
        self._fail_op = fail_op
        self._key_contains = key_contains

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if name != self._fail_op:
            return attr

        def wrapper(**kwargs):
            if self._key_contains in kwargs.get("Key", ""):
                raise ClientError(
                    {"Error": {"Code": "InternalError",
                               "Message": "simulated upload failure"}},
                    name,
                )
            return attr(**kwargs)

        return wrapper


def make_deployable_greengrass(component_state="DEPLOYABLE"):
    """A fake Use_Case-account greengrassv2 client."""
    gg = MagicMock(name="greengrassv2")
    gg.create_component_version.return_value = {
        "arn": f"arn:aws:greengrass:us-east-1:123456789012:components:test:versions:{uuid.uuid4()}"
    }
    gg.describe_component.return_value = {
        "status": {"componentState": component_state, "message": "simulated"}
    }
    return gg


def make_definition():
    """folder_source -> dewarp -> capture. dewarp requires the non-bundled
    'dda-dewarp' GStreamer plugin on every architecture, so packaging pulls
    plugins/{arch}/dda-dewarp.so from the curated plugin library."""
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


class PackagingEnv:
    """Per-test packaging harness: a validated workflow version, a
    Use_Case with an S3 bucket, the seeded plugin library, and patched
    Use_Case-account clients."""

    def __init__(self, env, packaging, monkeypatch):
        self.env = env
        self.packaging = packaging
        self.monkeypatch = monkeypatch
        self.s3 = env.s3

        # No 2-second polling waits in tests.
        monkeypatch.setattr(packaging, "COMPONENT_STATUS_POLL_SECONDS", 0)

        # A per-test plugin library prefix in the shared portal bucket, so
        # binaries seeded by one test never leak into another.
        self.plugin_prefix = f"{PLUGIN_PREFIX}-{uuid.uuid4()}"
        monkeypatch.setattr(
            packaging, "WORKFLOW_PLUGIN_LIBRARY_PREFIX", self.plugin_prefix)

        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_bucket = f"usecase-bucket-{uuid.uuid4()}"
        self.s3.create_bucket(Bucket=self.usecase_bucket)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Packaging Test",
            "account_id": "123456789012",
            "s3_bucket": self.usecase_bucket,
        })

        status, payload = env.invoke("POST", "/workflows", self.user, body={
            "usecase_id": self.usecase_id,
            "name": "packaged workflow",
            "definition": make_definition(),
        })
        assert status == 201, payload
        self.workflow_id = payload["workflow"]["workflow_id"]

        # Record a passed Workflow_Validator run so packaging is allowed.
        env.stack.tables.versions.update_item(
            Key={"workflow_id": self.workflow_id, "version": 1},
            UpdateExpression="SET validation_status = :v",
            ExpressionAttributeValues={
                ":v": {"status": "passed", "validated_at": 1,
                       "findings_key": "findings/none.json"},
            },
        )

    # ------------------------------------------------------------- setup
    def seed_plugin_library(self, archs):
        for arch in archs:
            self.s3.put_object(
                Bucket=self.env.bucket,
                Key=f"{self.plugin_prefix}/{arch}/dda-dewarp.so",
                Body=b"\x7fELF fake plugin " + arch.encode(),
            )

    def patch_usecase_clients(self, usecase_s3=None, greengrass=None):
        """Replace get_usecase_client in workflow_packaging so the
        cross-account role assumption is bypassed."""
        usecase_s3 = usecase_s3 if usecase_s3 is not None else self.s3
        greengrass = greengrass if greengrass is not None else make_deployable_greengrass()
        self.greengrass = greengrass

        def fake_get_usecase_client(service_name, usecase, session_name=None, region=None):
            assert usecase["usecase_id"] == self.usecase_id
            if service_name == "s3":
                return usecase_s3
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

    @property
    def staging_prefix(self):
        return f"{STAGING_ROOT}/{self.workflow_id}/"

    @property
    def final_prefix(self):
        return f"{COMPONENTS_ROOT}/{self.workflow_id}/1"

    def version_item(self):
        return self.env.stack.tables.versions.get_item(
            Key={"workflow_id": self.workflow_id, "version": 1})["Item"]

    def assert_nothing_packaged(self):
        """Requirement 7.5 post-failure state: no staged objects, no final
        artifacts, no registered component version, no packaging record."""
        assert self.keys_under(self.staging_prefix) == []
        assert self.keys_under(self.final_prefix) == []
        self.greengrass.create_component_version.assert_not_called()
        # The version record holds no component registration.
        assert not self.version_item().get("component_arn")


@pytest.fixture
def pkg_env(env, packaging, monkeypatch):
    return PackagingEnv(env, packaging, monkeypatch)


# --------------------------------------------------------------------------
# Failure at artifact upload (staging)
# --------------------------------------------------------------------------

class TestUploadFailure:
    def test_staging_upload_failure_cleans_stage_and_registers_nothing(self, pkg_env):
        """A failed upload of one architecture's zip deletes every staged
        object, reports that artifact, and registers no component (7.5)."""
        pkg_env.seed_plugin_library(["x86_64", "arm64_jp5"])
        flaky_s3 = FailingClientProxy(
            pkg_env.s3, fail_op="put_object", key_contains="arm64_jp5")
        pkg_env.patch_usecase_clients(usecase_s3=flaky_s3)

        status, payload = pkg_env.package(["x86_64", "arm64_jp5"])

        assert status == 502
        assert payload["error"]["code"] == "PACKAGING_FAILED"
        assert (payload["error"]["details"]["failing_artifact"]
                == "arm64_jp5/workflow-arm64_jp5.zip")
        # The x86_64 zip staged before the failure must not linger.
        pkg_env.assert_nothing_packaged()


# --------------------------------------------------------------------------
# Failure at promotion (staging -> final prefix)
# --------------------------------------------------------------------------

class TestPromoteFailure:
    def test_promote_failure_removes_already_promoted_artifacts(self, pkg_env):
        """When promotion fails after another architecture already promoted,
        both the stage and the partially promoted final prefix are deleted,
        the failing artifact is reported, and nothing registers (7.5)."""
        pkg_env.seed_plugin_library(["x86_64", "arm64_jp5"])
        flaky_s3 = FailingClientProxy(
            pkg_env.s3, fail_op="copy_object", key_contains="arm64_jp5")
        pkg_env.patch_usecase_clients(usecase_s3=flaky_s3)

        status, payload = pkg_env.package(["x86_64", "arm64_jp5"])

        assert status == 502
        assert payload["error"]["code"] == "PACKAGING_FAILED"
        assert (payload["error"]["details"]["failing_artifact"]
                == "arm64_jp5/workflow-arm64_jp5.zip")
        pkg_env.assert_nothing_packaged()


# --------------------------------------------------------------------------
# Failure assembling artifacts: missing plugin library binary
# --------------------------------------------------------------------------

class TestMissingPluginArtifact:
    def test_missing_plugin_so_identifies_artifact_and_registers_nothing(self, pkg_env):
        """A plugin binary absent from the curated library fails packaging
        with the plugins/{arch}/{plugin}.so artifact identified (7.5)."""
        # Deliberately no seed_plugin_library call.
        pkg_env.patch_usecase_clients()

        status, payload = pkg_env.package(["x86_64"])

        assert status == 502
        assert payload["error"]["code"] == "PACKAGING_FAILED"
        assert (payload["error"]["details"]["failing_artifact"]
                == "plugins/x86_64/dda-dewarp.so")
        assert "dda-dewarp" in payload["error"]["message"]
        pkg_env.assert_nothing_packaged()


# --------------------------------------------------------------------------
# Failure at component registration
# --------------------------------------------------------------------------

class TestRegistrationFailure:
    def test_create_component_version_error_cleans_all_uploaded_artifacts(self, pkg_env):
        """A registration API failure after successful uploads still deletes
        the stage and the promoted final artifacts (7.5)."""
        pkg_env.seed_plugin_library(["x86_64"])
        gg = make_deployable_greengrass()
        gg.create_component_version.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "CreateComponentVersion",
        )
        pkg_env.patch_usecase_clients(greengrass=gg)

        status, payload = pkg_env.package(["x86_64"])

        assert status == 502
        assert payload["error"]["code"] == "PACKAGING_FAILED"
        assert "component dda.workflow." in payload["error"]["details"]["failing_artifact"]
        assert pkg_env.keys_under(pkg_env.staging_prefix) == []
        assert pkg_env.keys_under(pkg_env.final_prefix) == []
        assert not pkg_env.version_item().get("component_arn")

    def test_non_deployable_component_version_is_deleted(self, pkg_env):
        """A registered version that never becomes DEPLOYABLE is deleted so
        no partial or broken component version remains (7.5)."""
        pkg_env.seed_plugin_library(["x86_64"])
        gg = make_deployable_greengrass(component_state="FAILED")
        pkg_env.patch_usecase_clients(greengrass=gg)

        status, payload = pkg_env.package(["x86_64"])

        assert status == 502
        assert payload["error"]["code"] == "PACKAGING_FAILED"
        assert "component dda.workflow." in payload["error"]["details"]["failing_artifact"]
        # The broken component version was removed from the registry.
        created_arn = gg.create_component_version.return_value["arn"]
        gg.delete_component.assert_called_once_with(arn=created_arn)
        # And no uploaded artifact remains anywhere.
        assert pkg_env.keys_under(pkg_env.staging_prefix) == []
        assert pkg_env.keys_under(pkg_env.final_prefix) == []
        assert not pkg_env.version_item().get("component_arn")


# --------------------------------------------------------------------------
# Success path: final artifacts persist, stage is cleaned, one registration
# --------------------------------------------------------------------------

class TestSuccessfulPackaging:
    def test_success_promotes_artifacts_cleans_stage_and_registers_once(self, pkg_env):
        pkg_env.seed_plugin_library(["x86_64", "arm64_jp5"])
        gg = pkg_env.patch_usecase_clients()

        status, payload = pkg_env.package(["x86_64", "arm64_jp5"])

        assert status == 201, payload
        assert payload["component_name"] == f"dda.workflow.{pkg_env.workflow_id}"
        assert payload["component_version"] == "1.0.0"

        # Final artifacts exist for both architectures; the stage is gone.
        final_keys = sorted(pkg_env.keys_under(pkg_env.final_prefix))
        assert final_keys == [
            f"{pkg_env.final_prefix}/arm64_jp5/workflow-arm64_jp5.zip",
            f"{pkg_env.final_prefix}/x86_64/workflow-x86_64.zip",
        ]
        assert pkg_env.keys_under(pkg_env.staging_prefix) == []

        # Exactly one component version registered, recorded on the version.
        gg.create_component_version.assert_called_once()
        gg.delete_component.assert_not_called()
        item = pkg_env.version_item()
        assert item["component_arn"] == gg.create_component_version.return_value["arn"]

        # The promoted zip is a complete artifact (manifest, definition,
        # compiled pipeline, and the plugin binary from the library).
        body = pkg_env.s3.get_object(
            Bucket=pkg_env.usecase_bucket,
            Key=f"{pkg_env.final_prefix}/x86_64/workflow-x86_64.zip",
        )["Body"].read()
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = set(zf.namelist())
            assert {"manifest.json", "workflow.json",
                    "compiled_pipeline.json",
                    "plugins/x86_64/dda-dewarp.so"} <= names
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["componentVersion"] == "1.0.0"
            assert manifest["pluginDependencies"] == ["dda-dewarp"]
