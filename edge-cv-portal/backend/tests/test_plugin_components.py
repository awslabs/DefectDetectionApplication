"""
Unit tests for plugin_components.py (custom-node-designer task 6.3).

Covers Plugin_Component auto-packaging (Requirements 16.1, 16.7):
install-only recipe assembly with one platform manifest per successfully
built Target_Architecture (ARCH_TO_GG_PLATFORM + 'variant' for JetPack
arm64, 'runtime: nvidia' for x86_64_nvidia, plain x86_64 ordered after
x86_64_nvidia), artifact staging/promotion to the Use_Case account
bucket (signed .so + plugin-manifest.json), registry tags, the
Plugin_Record 'component' status pointer, failed-registration cleanup,
retry idempotency (registered short-circuit, ConflictException
re-describe), and the GET /plugins/{id}/versions/{v}/component route.

Runs against the moto-backed stack from conftest.py with a MagicMock
Use_Case-account Greengrass client (moto does not implement
greengrassv2).
"""
import hashlib
import json
import sys
import uuid
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from conftest import TEST_ENV


@pytest.fixture(scope="module")
def components_module(aws_stack):
    """Import plugin_components inside the moto mock so its module-level
    boto3 clients (and workflow_packaging's) are intercepted."""
    for name in ("plugin_components", "workflow_packaging"):
        sys.modules.pop(name, None)
    import plugin_components

    return plugin_components


class PluginComponentsEnv:
    """Facade for exercising Plugin_Component packaging in tests."""

    def __init__(self, stack, module, monkeypatch):
        self.stack = stack
        self.module = module
        self.records = stack.plugin_records
        self.s3 = stack.s3
        self.bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(module, "COMPONENT_STATUS_POLL_SECONDS", 0)

        # Per-test Use_Case with its own account bucket.
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.usecase_bucket = f"usecase-bucket-{uuid.uuid4()}"
        self.s3.create_bucket(Bucket=self.usecase_bucket)
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Components Test Use Case",
            "account_id": "123456789012",
            "s3_bucket": self.usecase_bucket,
        })
        self.admin = self.make_user()
        self.assign_role(self.admin, "UseCaseAdmin")

    # ------------------------------------------------------------- setup
    def make_user(self, role="Viewer"):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    def assign_role(self, user, role):
        self.stack.tables.user_roles.put_item(Item={
            "user_id": user["user_id"],
            "usecase_id": self.usecase_id,
            "role": role,
        })

    def seed_plugin(self, built_archs, failed_archs=(), name="blur-regions"):
        """Create a Plugin_Record and record per-arch artifact entries with
        the .so bytes promoted to the portal Plugin_Library."""
        event = {
            "httpMethod": "POST",
            "resource": "/plugins",
            "path": "/plugins",
            "pathParameters": None,
            "queryStringParameters": None,
            "body": json.dumps({"usecase_id": self.usecase_id,
                                "name": name, "kind": "scaffold"}),
            "requestContext": {"authorizer": {"claims": {
                "sub": self.admin["user_id"],
                "email": self.admin["email"],
                "cognito:username": self.admin["username"],
                "custom:role": self.admin["role"],
            }}},
        }
        response = self.records.handler(event, None)
        assert response["statusCode"] == 201, response["body"]
        plugin = json.loads(response["body"])["plugin"]

        artifacts, self.so_bytes = {}, {}
        for arch in built_archs:
            data = f"\x7fELF {name} {arch}".encode()
            key = (f"workflow-plugins/custom/{self.usecase_id}/{arch}/"
                   f"{name}.so")
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)
            artifacts[arch] = {
                "buildStatus": "succeeded", "s3Key": key,
                "checksum": hashlib.sha256(data).hexdigest(),
                "signature": "c2ln", "logTail": "",
            }
            self.so_bytes[arch] = data
        for arch in failed_archs:
            artifacts[arch] = {"buildStatus": "failed",
                               "logTail": "compile error"}
        self.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin["plugin_id"], "version": plugin["version"]},
            UpdateExpression="SET artifacts = :a, requested_architectures = :r",
            ExpressionAttributeValues={
                ":a": artifacts,
                ":r": sorted(list(built_archs) + list(failed_archs)),
            },
        )
        return plugin

    def make_greengrass(self, state="DEPLOYABLE"):
        gg = MagicMock(name="greengrassv2")
        gg.meta.region_name = "us-east-1"
        gg.create_component_version.return_value = {
            "arn": ("arn:aws:greengrass:us-east-1:123456789012:components:"
                    f"test:versions:{uuid.uuid4()}")
        }
        gg.describe_component.return_value = {
            "status": {"componentState": state, "message": "simulated"}
        }
        return gg

    def patch_usecase_clients(self, greengrass=None):
        gg = greengrass or self.make_greengrass()
        moto_s3 = self.s3

        def fake_get_usecase_client(service_name, usecase, session_name=None,
                                    region=None):
            return {"s3": moto_s3, "greengrassv2": gg}[service_name]

        self.monkeypatch.setattr(self.module, "get_usecase_client",
                                 fake_get_usecase_client)
        return gg

    # ----------------------------------------------------------- invoke
    def package(self, plugin):
        return self.module.handler({
            "action": "package_plugin_component",
            "plugin_id": plugin["plugin_id"],
            "version": plugin["version"],
            "usecase_id": self.usecase_id,
        }, None)

    def get_component(self, user, plugin_id, version):
        event = {
            "httpMethod": "GET",
            "resource": "/plugins/{id}/versions/{v}/component",
            "path": f"/plugins/{plugin_id}/versions/{version}/component",
            "pathParameters": {"id": plugin_id, "v": str(version)},
            "queryStringParameters": None,
            "body": None,
            "requestContext": {"authorizer": {"claims": {
                "sub": user["user_id"],
                "email": user["email"],
                "cognito:username": user["username"],
                "custom:role": user["role"],
            }}},
        }
        response = self.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    # ------------------------------------------------------ conveniences
    def get_item(self, plugin):
        return self.records.get_version_item(plugin["plugin_id"],
                                             plugin["version"])

    def account_keys(self, prefix):
        listed = self.s3.list_objects_v2(Bucket=self.usecase_bucket,
                                         Prefix=prefix)
        return sorted(o["Key"] for o in listed.get("Contents", []))

    def sent_recipe(self, gg):
        kwargs = gg.create_component_version.call_args.kwargs
        return json.loads(kwargs["inlineRecipe"]), kwargs.get("tags", {})


@pytest.fixture
def cenv(aws_stack, components_module, monkeypatch):
    return PluginComponentsEnv(aws_stack, components_module, monkeypatch)


class TestRecipeAssembly:
    """build_plugin_recipe is pure over the built architectures (16.1)."""

    def test_one_manifest_per_built_architecture(self, components_module):
        recipe = components_module.build_plugin_recipe(
            "plg-1", 3, "acct-bucket",
            {"x86_64": "p.so", "arm64_jp5": "p.so"})

        assert recipe["ComponentName"] == "dda.plugin.plg-1"
        assert recipe["ComponentVersion"] == "3.0.0"
        platforms = [m["Platform"] for m in recipe["Manifests"]]
        assert [p["architecture"] for p in platforms] == ["aarch64", "amd64"]

    def test_install_only_lifecycle_and_install_path(self, components_module):
        recipe = components_module.build_plugin_recipe(
            "plg-1", 2, "acct-bucket", {"x86_64": "blur.so"})

        assert recipe["Lifecycle"] == {}  # no top-level Run lifecycle
        manifest = recipe["Manifests"][0]
        assert list(manifest["Lifecycle"].keys()) == ["Install"]
        script = manifest["Lifecycle"]["Install"]["Script"]
        assert "/aws_dda/plugins/plg-1/2/x86_64" in script
        uris = [a["Uri"] for a in manifest["Artifacts"]]
        assert uris == [
            "s3://acct-bucket/plugins/components/plg-1/2/x86_64/blur.so",
            "s3://acct-bucket/plugins/components/plg-1/2/x86_64/plugin-manifest.json",
        ]

    def test_platform_attributes_variant_and_nvidia_runtime(self, components_module):
        recipe = components_module.build_plugin_recipe(
            "plg-1", 1, "b",
            {a: "p.so" for a in
             ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6")})

        by_arch = {}
        for manifest in recipe["Manifests"]:
            platform = manifest["Platform"]
            if platform["architecture"] == "aarch64":
                by_arch[platform["variant"]] = platform
            elif platform.get("runtime") == "nvidia":
                by_arch["x86_64_nvidia"] = platform
            else:
                by_arch["x86_64"] = platform
        assert set(by_arch) == {"x86_64", "x86_64_nvidia",
                                "arm64_jp4", "arm64_jp5", "arm64_jp6"}
        for jp in ("arm64_jp4", "arm64_jp5", "arm64_jp6"):
            assert by_arch[jp] == {"os": "linux", "architecture": "aarch64",
                                   "variant": jp}
        assert by_arch["x86_64_nvidia"] == {"os": "linux",
                                            "architecture": "amd64",
                                            "runtime": "nvidia"}
        assert by_arch["x86_64"] == {"os": "linux", "architecture": "amd64"}

    def test_plain_x86_64_manifest_ordered_after_x86_64_nvidia(self, components_module):
        order = components_module.manifest_arch_order(
            ["x86_64", "arm64_jp6", "x86_64_nvidia"])
        assert order == ["arm64_jp6", "x86_64_nvidia", "x86_64"]

        recipe = components_module.build_plugin_recipe(
            "plg-1", 1, "b", {"x86_64": "p.so", "x86_64_nvidia": "p.so"})
        runtimes = [m["Platform"].get("runtime") for m in recipe["Manifests"]]
        assert runtimes == ["nvidia", None]


class TestAutoPackaging:
    """Async packaging trigger: stage -> promote -> register (16.1)."""

    def test_successful_packaging_registers_and_records_pointer(self, cenv):
        plugin = cenv.seed_plugin(["x86_64", "arm64_jp5"],
                                  failed_archs=["arm64_jp4"])
        gg = cenv.patch_usecase_clients()

        result = cenv.package(plugin)

        assert result["packaged"] is True, result
        assert result["component_name"] == f"dda.plugin.{plugin['plugin_id']}"
        assert result["component_version"] == f"{plugin['version']}.0.0"
        # Manifests cover exactly the successfully built architectures.
        recipe, tags = cenv.sent_recipe(gg)
        assert len(recipe["Manifests"]) == 2
        assert tags == {
            "dda-portal:managed": "true",
            "dda-portal:usecase-id": cenv.usecase_id,
            "dda-portal:plugin-id": plugin["plugin_id"],
            "dda-portal:plugin-version": str(plugin["version"]),
        }
        # Component pointer recorded on the Plugin_Record.
        component = cenv.get_item(plugin)["component"]
        assert component["status"] == "registered"
        assert component["arn"] == result["component_arn"]
        assert component["architectures"] == ["arm64_jp5", "x86_64"]

    def test_artifacts_promoted_to_account_bucket_and_stage_cleaned(self, cenv):
        plugin = cenv.seed_plugin(["x86_64"])
        cenv.patch_usecase_clients()
        pid, ver = plugin["plugin_id"], plugin["version"]

        result = cenv.package(plugin)

        assert result["packaged"] is True, result
        final_prefix = f"plugins/components/{pid}/{ver}/x86_64/"
        assert cenv.account_keys(final_prefix) == [
            final_prefix + "blur-regions.so",
            final_prefix + "plugin-manifest.json",
        ]
        # Signed .so bytes copied verbatim; plugin-manifest.json carries
        # name, version, arch, and checksum (16.1).
        so = cenv.s3.get_object(Bucket=cenv.usecase_bucket,
                                Key=final_prefix + "blur-regions.so")
        assert so["Body"].read() == cenv.so_bytes["x86_64"]
        manifest = json.loads(cenv.s3.get_object(
            Bucket=cenv.usecase_bucket,
            Key=final_prefix + "plugin-manifest.json")["Body"].read())
        assert manifest == {
            "name": "blur-regions",
            "version": ver,
            "arch": "x86_64",
            "checksum": hashlib.sha256(cenv.so_bytes["x86_64"]).hexdigest(),
        }
        assert cenv.account_keys(f"plugins/staging/{pid}/") == []

    def test_failed_registration_deletes_component_and_artifacts(self, cenv):
        """A version that never becomes DEPLOYABLE is deleted and no
        promoted artifacts remain (all-or-nothing)."""
        plugin = cenv.seed_plugin(["x86_64"])
        gg = cenv.patch_usecase_clients(cenv.make_greengrass(state="BROKEN"))
        pid, ver = plugin["plugin_id"], plugin["version"]

        result = cenv.package(plugin)

        assert result["packaged"] is False
        gg.delete_component.assert_called_once_with(
            arn=gg.create_component_version.return_value["arn"])
        assert cenv.account_keys(f"plugins/components/{pid}/{ver}/") == []
        assert cenv.account_keys(f"plugins/staging/{pid}/") == []
        component = cenv.get_item(plugin)["component"]
        assert component["status"] == "failed"
        assert "DEPLOYABLE" in component["failure"]

    def test_registered_pointer_short_circuits_retry(self, cenv):
        plugin = cenv.seed_plugin(["x86_64"])
        gg = cenv.patch_usecase_clients()

        first = cenv.package(plugin)
        retry = cenv.package(plugin)

        assert first["packaged"] is True
        assert retry["packaged"] is True
        assert retry["short_circuited"] is True
        assert retry["component_arn"] == first["component_arn"]
        assert gg.create_component_version.call_count == 1

    def test_conflict_exception_re_describes_existing_version(self, cenv):
        """ConflictException (already registered by a previous attempt)
        resolves idempotently without deleting the existing version (16.7)."""
        plugin = cenv.seed_plugin(["x86_64"])
        gg = cenv.make_greengrass()
        gg.create_component_version.side_effect = ClientError(
            {"Error": {"Code": "ConflictException", "Message": "exists"}},
            "CreateComponentVersion")
        cenv.patch_usecase_clients(gg)

        result = cenv.package(plugin)

        assert result["packaged"] is True, result
        expected_arn = ("arn:aws:greengrass:us-east-1:123456789012:components:"
                        f"dda.plugin.{plugin['plugin_id']}:versions:"
                        f"{plugin['version']}.0.0")
        assert result["component_arn"] == expected_arn
        gg.delete_component.assert_not_called()
        assert cenv.get_item(plugin)["component"]["status"] == "registered"

    def test_no_successful_builds_records_nothing(self, cenv):
        plugin = cenv.seed_plugin([], failed_archs=["x86_64"])
        gg = cenv.patch_usecase_clients()

        result = cenv.package(plugin)

        assert result == {"packaged": False, "reason": "no successful builds"}
        gg.create_component_version.assert_not_called()

    def test_missing_record_never_raises(self, cenv):
        result = cenv.module.handler({
            "action": "package_plugin_component",
            "plugin_id": "no-such-plugin", "version": 1,
            "usecase_id": cenv.usecase_id,
        }, None)
        assert result["packaged"] is False


class TestComponentStatusEndpoint:
    """GET /plugins/{id}/versions/{v}/component."""

    def test_viewer_reads_component_pointer(self, cenv):
        plugin = cenv.seed_plugin(["x86_64"])
        cenv.patch_usecase_clients()
        cenv.package(plugin)
        viewer = cenv.make_user()
        cenv.assign_role(viewer, "Viewer")

        status, body = cenv.get_component(viewer, plugin["plugin_id"],
                                          plugin["version"])

        assert status == 200
        assert body["component"]["status"] == "registered"
        assert body["component"]["name"] == f"dda.plugin.{plugin['plugin_id']}"
        assert body["component"]["architectures"] == ["x86_64"]

    def test_unpackaged_version_returns_empty_pointer(self, cenv):
        plugin = cenv.seed_plugin(["x86_64"])

        status, body = cenv.get_component(cenv.admin, plugin["plugin_id"],
                                          plugin["version"])

        assert status == 200
        assert body["component"] == {}

    def test_missing_record_returns_404(self, cenv):
        status, body = cenv.get_component(cenv.admin, "no-such-plugin", 1)
        assert status == 404
        assert body["error"]["code"] == "PLUGIN_NOT_FOUND"
