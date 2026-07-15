"""Property test for Plugin_Component version immutability (task 6.5).

**Feature: custom-node-designer, Property 23: Plugin_Component versions are immutable under rebuild**

For all sequences of source-change and rebuild operations on a plugin,
every publish produces a Plugin_Component version not previously
registered, and the recipes and artifact references of all previously
published Plugin_Component versions are unchanged after each publish.

**Validates: Requirements 16.7**

Each generated sequence exercises the real production path against the
moto-backed stack: a rebuild/source change creates a new Plugin_Record
version through PUT /plugins/{id} with new_version=true (plugin_records
handler), the new version's per-arch build artifacts are recorded (with
fresh .so bytes overwriting the shared Plugin_Library keys, exactly as a
rebuild would), and the version is packaged through plugin_components'
package_plugin_component. After every publish (including idempotent
retries of already-published versions) the test asserts:

  (a) each previously published version's `component` pointer (name,
      component version, ARN, status) and its promoted artifact bytes
      under plugins/components/{pluginId}/{priorVersion}/... are
      byte-identical to the snapshot taken when it was first published;
  (b) the Use_Case-account Greengrass registry (a MagicMock; moto does
      not implement greengrassv2) never receives delete_component for
      any version, and never receives a second create_component_version
      for an already-registered (ComponentName, ComponentVersion);
  (c) every publish registers a component version string and ARN that
      were not previously registered.
"""

import hashlib
import json
import sys
import uuid
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from conftest import TEST_ENV

ARCHS = ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6")


@pytest.fixture(scope="module")
def components_module(aws_stack):
    """Import plugin_components inside the moto mock so its module-level
    boto3 clients (and workflow_packaging's) are intercepted."""
    for name in ("plugin_components", "workflow_packaging"):
        sys.modules.pop(name, None)
    import plugin_components

    return plugin_components


class ImmutabilityEnv:
    """Facade driving rebuild -> new version -> package sequences."""

    def __init__(self, stack, module, monkeypatch):
        self.stack = stack
        self.module = module
        self.records = stack.plugin_records
        self.s3 = stack.s3
        self.bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]
        monkeypatch.setattr(module, "COMPONENT_STATUS_POLL_SECONDS", 0)

        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.usecase_bucket = f"usecase-bucket-{uuid.uuid4()}"
        self.s3.create_bucket(Bucket=self.usecase_bucket)
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Immutability Property Use Case",
            "account_id": "123456789012",
            "s3_bucket": self.usecase_bucket,
        })

        user_id = f"user-{uuid.uuid4()}"
        self.admin = {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": "UseCaseAdmin",
        }
        stack.tables.user_roles.put_item(Item={
            "user_id": user_id,
            "usecase_id": self.usecase_id,
            "role": "UseCaseAdmin",
        })

        # Rebound per example (fresh registry mock per generated sequence).
        self.gg = None
        moto_s3 = self.s3

        def fake_get_usecase_client(service_name, usecase, session_name=None,
                                    region=None):
            return {"s3": moto_s3, "greengrassv2": self.gg}[service_name]

        monkeypatch.setattr(module, "get_usecase_client",
                            fake_get_usecase_client)

    # ----------------------------------------------------- registry mock
    def fresh_greengrass(self):
        """MagicMock greengrassv2 whose registry records every created
        (ComponentName, ComponentVersion) and mints per-version ARNs, so
        duplicate registration of an existing version is detectable."""
        gg = MagicMock(name="greengrassv2")
        gg.meta.region_name = "us-east-1"
        gg.registered = {}  # (name, version) -> recipe dict

        def create_component_version(inlineRecipe=None, tags=None, **kwargs):
            recipe = json.loads(inlineRecipe)
            key = (recipe["ComponentName"], recipe["ComponentVersion"])
            # (b) an already-registered version is never re-created:
            # rebuilds publish strictly new versions (16.7).
            assert key not in gg.registered, (
                f"create_component_version called again for already "
                f"registered {key}")
            gg.registered[key] = recipe
            return {"arn": ("arn:aws:greengrass:us-east-1:123456789012:"
                            f"components:{key[0]}:versions:{key[1]}")}

        gg.create_component_version.side_effect = create_component_version
        gg.describe_component.return_value = {
            "status": {"componentState": "DEPLOYABLE", "message": "ok"}
        }
        self.gg = gg
        return gg

    # ------------------------------------------------- record operations
    def _event(self, method, resource, path_params, body):
        return {
            "httpMethod": method,
            "resource": resource,
            "path": resource,
            "pathParameters": path_params,
            "queryStringParameters": None,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {
                "sub": self.admin["user_id"],
                "email": self.admin["email"],
                "cognito:username": self.admin["username"],
                "custom:role": self.admin["role"],
            }}},
        }

    def create_plugin(self, name):
        response = self.records.handler(self._event(
            "POST", "/plugins", None,
            {"usecase_id": self.usecase_id, "name": name,
             "kind": "scaffold"}), None)
        assert response["statusCode"] == 201, response["body"]
        return json.loads(response["body"])["plugin"]

    def rebuild(self, plugin):
        """Source change / rebuild: a new Plugin_Record version via
        PUT /plugins/{id} new_version=true (the existing design 16.7
        leans on)."""
        response = self.records.handler(self._event(
            "PUT", "/plugins/{id}", {"id": plugin["plugin_id"]},
            {"new_version": True}), None)
        assert response["statusCode"] == 201, response["body"]
        return json.loads(response["body"])["plugin"]

    def record_build_results(self, plugin, built, failed, payload):
        """Record per-arch artifact entries as the build result handler
        would, promoting fresh .so bytes to the (unversioned, shared)
        Plugin_Library keys - overwriting what prior versions put there,
        exactly like a real rebuild."""
        name = plugin["name"]
        artifacts = {}
        for arch in sorted(built):
            data = (b"\x7fELF " + payload +
                    f" {name} v{plugin['version']} {arch}".encode())
            key = (f"workflow-plugins/custom/{self.usecase_id}/{arch}/"
                   f"{name}.so")
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)
            artifacts[arch] = {
                "buildStatus": "succeeded", "s3Key": key,
                "checksum": hashlib.sha256(data).hexdigest(),
                "signature": "c2ln", "logTail": "",
            }
        for arch in sorted(failed):
            artifacts[arch] = {"buildStatus": "failed",
                               "logTail": "compile error"}
        self.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin["plugin_id"],
                 "version": plugin["version"]},
            UpdateExpression="SET artifacts = :a, requested_architectures = :r",
            ExpressionAttributeValues={
                ":a": artifacts,
                ":r": sorted(set(built) | set(failed)),
            },
        )

    def package(self, plugin):
        return self.module.handler({
            "action": "package_plugin_component",
            "plugin_id": plugin["plugin_id"],
            "version": plugin["version"],
            "usecase_id": self.usecase_id,
        }, None)

    # -------------------------------------------------------- snapshots
    def read_component_prefix(self, plugin):
        """{key: bytes} of one version's promoted component artifacts."""
        prefix = (f"plugins/components/{plugin['plugin_id']}/"
                  f"{plugin['version']}/")
        listed = self.s3.list_objects_v2(Bucket=self.usecase_bucket,
                                         Prefix=prefix)
        return {
            obj["Key"]: self.s3.get_object(
                Bucket=self.usecase_bucket,
                Key=obj["Key"])["Body"].read()
            for obj in listed.get("Contents", [])
        }

    def snapshot(self, plugin):
        """Everything 16.7 says must stay frozen once published."""
        item = self.records.get_version_item(plugin["plugin_id"],
                                             plugin["version"])
        objects = self.read_component_prefix(plugin)
        assert objects, "published version has no promoted artifacts"
        return {"plugin": plugin, "component": item["component"],
                "objects": objects}

    def assert_unchanged(self, snap):
        """(a) The pointer and every promoted artifact byte of a
        previously published version are exactly as first published."""
        plugin = snap["plugin"]
        item = self.records.get_version_item(plugin["plugin_id"],
                                             plugin["version"])
        assert item["component"] == snap["component"], (
            f"component pointer of published v{plugin['version']} changed")
        assert self.read_component_prefix(plugin) == snap["objects"], (
            f"promoted artifacts of published v{plugin['version']} changed")


@pytest.fixture
def ienv(aws_stack, components_module, monkeypatch):
    return ImmutabilityEnv(aws_stack, components_module, monkeypatch)


# ---------------------------------------------------------------------------
# Operation sequences: each step is one source-change/rebuild followed by a
# publish, with a random per-step architecture outcome, fresh source bytes,
# and an optional idempotent re-package of a previously published version.
# ---------------------------------------------------------------------------

_step = st.fixed_dictionaries({
    "built": st.sets(st.sampled_from(ARCHS), min_size=1, max_size=3),
    "failed": st.sets(st.sampled_from(ARCHS), max_size=2),
    "payload": st.binary(min_size=1, max_size=16),
    # >= 0 selects a previously published version (mod count) to
    # re-package after this step's publish; -1 skips the retry.
    "retry": st.integers(min_value=-1, max_value=99),
})

_steps = st.lists(_step, min_size=1, max_size=3)


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(steps=_steps)
def test_plugin_component_versions_immutable_under_rebuild(ienv, steps):
    """**Feature: custom-node-designer, Property 23: Plugin_Component versions are immutable under rebuild**

    For all sequences of source-change and rebuild operations on a
    plugin, every publish produces a Plugin_Component version not
    previously registered, and the recipes and artifact references of
    all previously published Plugin_Component versions are unchanged
    after each publish.

    **Validates: Requirements 16.7**
    """
    env = ienv
    gg = env.fresh_greengrass()
    plugin = env.create_plugin(f"immutable-{uuid.uuid4().hex[:8]}")

    published = []          # snapshots, in publish order
    seen_versions = set()   # registered ComponentVersion strings
    seen_arns = set()       # registered component version ARNs

    for i, step in enumerate(steps):
        if i > 0:
            # Rebuild / source change -> a new Plugin_Record version.
            plugin = env.rebuild(plugin)
        env.record_build_results(plugin, step["built"],
                                 step["failed"] - step["built"],
                                 step["payload"])

        result = env.package(plugin)
        assert result["packaged"] is True, result

        # (c) Every publish is a not-previously-registered version.
        comp_version = result["component_version"]
        assert comp_version == f"{plugin['version']}.0.0"
        assert comp_version not in seen_versions
        assert result["component_arn"] not in seen_arns
        seen_versions.add(comp_version)
        seen_arns.add(result["component_arn"])

        # The registry recipe of this publish references only this
        # version's artifact prefix, never a prior version's.
        recipe = gg.registered[(result["component_name"], comp_version)]
        own_prefix = (f"plugins/components/{plugin['plugin_id']}/"
                      f"{plugin['version']}/")
        for manifest in recipe["Manifests"]:
            for artifact in manifest["Artifacts"]:
                assert (f"s3://{env.usecase_bucket}/{own_prefix}"
                        in artifact["Uri"])

        published.append(env.snapshot(plugin))

        # Optional idempotent retry of an already published version:
        # short-circuits without touching the registry or artifacts.
        if step["retry"] >= 0:
            prior = published[step["retry"] % len(published)]
            retry = env.package(prior["plugin"])
            assert retry["packaged"] is True, retry
            assert retry.get("short_circuited") is True
            assert retry["component_arn"] == prior["component"]["arn"]

        # (b) No published component version is ever deleted, and
        # (a) every previously published version is byte-identical.
        gg.delete_component.assert_not_called()
        for snap in published:
            env.assert_unchanged(snap)

    # Final sweep: after the whole sequence every published version is
    # still exactly as first published.
    gg.delete_component.assert_not_called()
    for snap in published:
        env.assert_unchanged(snap)
