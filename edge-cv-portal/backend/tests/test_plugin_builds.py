"""
Unit tests for plugin_builds.py (custom-node-designer task 6.2).

Covers build submission (per-arch building status + StartBuild, 3.1),
the EventBridge result handler (idempotent per-arch artifact recording:
SHA-256 checksum, KMS signature, Plugin_Library promotion on success,
CloudWatch log tail with no artifact on failure — 3.3, 3.4), the
prebuilt-binary upload path (3.6), the DeepStream architecture
restriction (5.1), the per-arch build status endpoint (3.5), and the
plugin_components.py trigger on settlement with >= 1 success (16.1
hand-off; absence of the function never fails the build).

Runs against the moto-backed stack from conftest.py (real KMS ECC key,
real CodeBuild projects, real DynamoDB/S3).
"""
import base64
import hashlib
import json
import time
import uuid

import pytest

from conftest import TEST_ENV


class PluginBuildsEnv:
    """Facade for invoking the Plugin_Build_Service API in tests."""

    def __init__(self, stack):
        self.stack = stack
        self.module = stack.plugin_builds
        self.records = stack.plugin_records
        self.s3 = stack.s3
        self.kms = stack.kms
        self.bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]

    # ------------------------------------------------------------- setup
    def create_usecase(self):
        usecase_id = f"uc-{uuid.uuid4()}"
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": usecase_id,
            "name": "Builds Test Use Case",
            "account_id": "123456789012",
        })
        return usecase_id

    def make_user(self, role="Viewer"):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    def assign_role(self, user, usecase_id, role):
        self.stack.tables.user_roles.put_item(Item={
            "user_id": user["user_id"],
            "usecase_id": usecase_id,
            "role": role,
        })

    def make_admin(self, usecase_id):
        admin = self.make_user()
        self.assign_role(admin, usecase_id, "UseCaseAdmin")
        return admin

    def create_plugin(self, user, usecase_id, name="blur-regions", **extra):
        status, body = self.invoke_records(
            "POST", "/plugins", user,
            body={"usecase_id": usecase_id, "name": name,
                  "kind": "scaffold", **extra})
        assert status == 201, body
        return body["plugin"]

    def get_item(self, plugin_id, version):
        return self.records.get_version_item(plugin_id, version)

    # ----------------------------------------------------------- invoke
    def _event(self, method, resource, user, plugin_id, version, body=None):
        return {
            "httpMethod": method,
            "resource": resource,
            "path": resource.replace("{id}", plugin_id or "")
                            .replace("{v}", str(version or "")),
            "pathParameters": {"id": plugin_id, "v": str(version)},
            "queryStringParameters": None,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "sub": user["user_id"],
                        "email": user["email"],
                        "cognito:username": user["username"],
                        "custom:role": user["role"],
                    }
                }
            },
        }

    def invoke_records(self, method, resource, user, plugin_id=None,
                       version=None, body=None):
        event = self._event(method, resource, user, plugin_id, version, body)
        response = self.records.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def post_build(self, user, plugin_id, version, body=None):
        event = self._event("POST", "/plugins/{id}/versions/{v}/build",
                            user, plugin_id, version, body or {})
        response = self.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def get_builds(self, user, plugin_id, version):
        event = self._event("GET", "/plugins/{id}/versions/{v}/builds",
                            user, plugin_id, version)
        response = self.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    # ------------------------------------------------------- EventBridge
    def codebuild_event(self, arch, build_id, status, plugin_id, version,
                        usecase_id, plugin_name, logs=None):
        project = f"dda-plugin-build-{arch}"
        env = {
            "USECASE_ID": usecase_id,
            "PLUGIN_ID": plugin_id,
            "PLUGIN_VERSION": str(version),
            "PLUGIN_NAME": plugin_name,
            "TARGET_ARCH": arch,
        }
        return {
            "source": "aws.codebuild",
            "detail-type": "CodeBuild Build State Change",
            "detail": {
                "build-status": status,
                "project-name": project,
                "build-id": (
                    f"arn:aws:codebuild:us-east-1:123456789012:build/{build_id}"),
                "additional-information": {
                    "environment": {
                        "environment-variables": [
                            {"name": k, "value": v} for k, v in env.items()
                        ],
                    },
                    "logs": logs or {},
                },
            },
        }

    def deliver_result(self, **kwargs):
        return self.module.handler(self.codebuild_event(**kwargs), None)

    # ------------------------------------------------------ conveniences
    def library_key(self, usecase_id, arch, plugin_name):
        """The UNVERSIONED promotion key the build image writes to."""
        return f"workflow-plugins/custom/{usecase_id}/{arch}/{plugin_name}.so"

    def versioned_library_key(self, usecase_id, arch, plugin_id, version,
                              plugin_name):
        """The IMMUTABLE per-version key the recorded artifact is homed to
        (defect 8): later-version rebuilds overwrite the promotion key but
        never this one, so each version stays verifiable forever."""
        return (f"workflow-plugins/custom/{usecase_id}/{arch}/"
                f"{plugin_id}/{int(version)}/{plugin_name}.so")

    def put_promoted_artifact(self, usecase_id, arch, plugin_name, data):
        key = self.library_key(usecase_id, arch, plugin_name)
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)
        return key

    def submit_and_succeed(self, admin, plugin, arch="x86_64",
                           data=b"\x7fELF-shared-object"):
        """POST /build for one arch, then deliver its SUCCEEDED result."""
        plugin_id, version = plugin["plugin_id"], plugin["version"]
        status, body = self.post_build(admin, plugin_id, version,
                                       {"architectures": [arch]})
        assert status == 202, body
        item = self.get_item(plugin_id, version)
        build_id = item["artifacts"][arch]["buildId"]
        self.put_promoted_artifact(plugin["usecase_id"], arch,
                                   "blur-regions", data)
        result = self.deliver_result(
            arch=arch, build_id=build_id, status="SUCCEEDED",
            plugin_id=plugin_id, version=version,
            usecase_id=plugin["usecase_id"], plugin_name="blur-regions")
        return build_id, result


@pytest.fixture
def benv(aws_stack):
    return PluginBuildsEnv(aws_stack)


class TestBuildSubmission:
    """POST /plugins/{id}/versions/{v}/build (3.1)."""

    def test_marks_building_and_starts_one_build_per_architecture(self, benv):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        archs = ["x86_64", "arm64_jp5"]

        status, body = benv.post_build(admin, plugin["plugin_id"],
                                       plugin["version"],
                                       {"architectures": archs})

        assert status == 202
        assert body["requested_architectures"] == sorted(archs)
        item = benv.get_item(plugin["plugin_id"], plugin["version"])
        started_ids = []
        for arch in archs:
            entry = item["artifacts"][arch]
            assert entry["buildStatus"] == "building"
            assert entry["buildId"].startswith(f"dda-plugin-build-{arch}:")
            started_ids.append(entry["buildId"])
        # The CodeBuild builds actually exist (StartBuild was called).
        builds = benv.stack.codebuild.batch_get_builds(ids=started_ids)["builds"]
        assert len(builds) == 2

    def test_defaults_to_previously_requested_architectures(self, benv):
        """Queued entries (e.g. from an import) build without an explicit
        architectures list."""
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        benv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin["plugin_id"], "version": plugin["version"]},
            UpdateExpression="SET requested_architectures = :r, artifacts = :a",
            ExpressionAttributeValues={
                ":r": ["arm64_jp6"],
                ":a": {"arm64_jp6": {"buildStatus": "queued"}},
            },
        )

        status, body = benv.post_build(admin, plugin["plugin_id"],
                                       plugin["version"], {})

        assert status == 202
        assert body["builds"]["arm64_jp6"]["buildStatus"] == "building"

    def test_rejects_unknown_architecture(self, benv):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)

        status, body = benv.post_build(admin, plugin["plugin_id"],
                                       plugin["version"],
                                       {"architectures": ["sparc"]})

        assert status == 400
        assert body["error"]["code"] == "INVALID_ARCHITECTURES"

    def test_deepstream_record_restricted_to_jetpack_architectures(self, benv):
        """DeepStream-flagged records may only select arm64_jp4/jp5/jp6 (5.1)."""
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id, deepstream=True)

        status, body = benv.post_build(admin, plugin["plugin_id"],
                                       plugin["version"],
                                       {"architectures": ["x86_64", "arm64_jp5"]})
        assert status == 400
        assert body["error"]["code"] == "INVALID_ARCHITECTURES"
        assert body["error"]["details"]["invalid"] == ["x86_64"]

        status, _ = benv.post_build(admin, plugin["plugin_id"],
                                    plugin["version"],
                                    {"architectures": ["arm64_jp4", "arm64_jp6"]})
        assert status == 202

    def test_viewer_cannot_submit_builds(self, benv):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        viewer = benv.make_user()
        benv.assign_role(viewer, usecase_id, "Viewer")

        status, body = benv.post_build(viewer, plugin["plugin_id"],
                                       plugin["version"],
                                       {"architectures": ["x86_64"]})

        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"


class TestBuildResults:
    """EventBridge result recording (3.3, 3.4), idempotent on build id."""

    def test_success_records_checksum_signature_and_library_key(self, benv):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        data = b"\x7fELF fake shared object bytes"

        _, result = benv.submit_and_succeed(admin, plugin, "x86_64", data)

        assert result["recorded"] is True
        item = benv.get_item(plugin["plugin_id"], plugin["version"])
        entry = item["artifacts"]["x86_64"]
        so_key = benv.versioned_library_key(
            usecase_id, "x86_64", plugin["plugin_id"], plugin["version"],
            "blur-regions")
        assert entry["buildStatus"] == "succeeded"
        # The recorded artifact is homed to the immutable per-version key,
        # not the overwritten unversioned promotion key (defect 8).
        assert entry["s3Key"] == so_key
        assert entry["checksum"] == hashlib.sha256(data).hexdigest()
        # The signature verifies against the artifact digest with the
        # portal signing key (3.3).
        verified = benv.kms.verify(
            KeyId=TEST_ENV.get("PLUGIN_SIGNING_KEY_ARN")
            or benv.module.PLUGIN_SIGNING_KEY_ARN,
            Message=hashlib.sha256(data).digest(),
            MessageType="DIGEST",
            Signature=base64.b64decode(entry["signature"]),
            SigningAlgorithm="ECDSA_SHA_256",
        )
        assert verified["SignatureValid"] is True
        # The detached signature was stored alongside the artifact.
        sig = benv.s3.get_object(Bucket=benv.bucket, Key=so_key + ".sig")
        assert sig["Body"].read() == base64.b64decode(entry["signature"])

    def test_later_version_rebuild_preserves_earlier_versions_artifact(
            self, benv):
        """Defect 8 invariant: building a LATER version of a plugin must
        NOT invalidate an EARLIER version's recorded artifact.

        Every build promotes to the same unversioned Plugin_Library key,
        so before the fix a v2 build overwrote the bytes v1's record
        still pointed at and v1 failed Requirement 10.4 checksum
        verification at packaging. Re-homing each build's bytes to an
        immutable per-version key means v1's recorded s3Key keeps v1's
        bytes — and stays verifiable against v1's recorded checksum —
        after any number of later-version rebuilds overwrite the shared
        promotion key.
        """
        usecase_id = benv.create_usecase()
        plugin_id = "plg-defect8"
        plugin_name = "blur-regions"
        arch = "arm64_jp6"
        v1_bytes = b"\x7fELF version-one artifact bytes"
        v2_bytes = b"\x7fELF version-two DIFFERENT artifact bytes"

        # v1 builds: promotion key holds v1's bytes; record + re-home.
        benv.put_promoted_artifact(usecase_id, arch, plugin_name, v1_bytes)
        v1 = benv.module.record_promoted_artifact(
            usecase_id, arch, plugin_name, plugin_id, 1)

        # v2 builds LATER, overwriting the SAME unversioned promotion key.
        benv.put_promoted_artifact(usecase_id, arch, plugin_name, v2_bytes)
        v2 = benv.module.record_promoted_artifact(
            usecase_id, arch, plugin_name, plugin_id, 2)

        # Each version recorded a distinct, version-scoped key.
        assert v1["s3Key"] != v2["s3Key"]
        assert v1["s3Key"] == benv.versioned_library_key(
            usecase_id, arch, plugin_id, 1, plugin_name)
        assert v2["s3Key"] == benv.versioned_library_key(
            usecase_id, arch, plugin_id, 2, plugin_name)

        # v1's recorded bytes/checksum survive v2's rebuild (the pre-fix
        # Req 10.4 packaging rejection of v1 can no longer occur).
        v1_stored = benv.s3.get_object(
            Bucket=benv.bucket, Key=v1["s3Key"])["Body"].read()
        assert v1_stored == v1_bytes
        assert v1["checksum"] == hashlib.sha256(v1_bytes).hexdigest()

        # v2 is independently intact at its own key.
        v2_stored = benv.s3.get_object(
            Bucket=benv.bucket, Key=v2["s3Key"])["Body"].read()
        assert v2_stored == v2_bytes
        assert v2["checksum"] == hashlib.sha256(v2_bytes).hexdigest()

    def test_failure_records_log_tail_and_no_artifact(self, benv):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        plugin_id, version = plugin["plugin_id"], plugin["version"]
        benv.post_build(admin, plugin_id, version,
                        {"architectures": ["arm64_jp4"]})
        build_id = benv.get_item(plugin_id, version)["artifacts"]["arm64_jp4"]["buildId"]

        # Stage the CloudWatch build log the handler tails (3.4).
        logs = benv.stack.plugin_builds.logs_client
        group, stream = "/aws/codebuild/dda-plugin-build-arm64_jp4", build_id.split(":")[1]
        logs.create_log_group(logGroupName=group)
        logs.create_log_stream(logGroupName=group, logStreamName=stream)
        now = int(time.time() * 1000)
        logs.put_log_events(logGroupName=group, logStreamName=stream, logEvents=[
            {"timestamp": now, "message": "meson setup build"},
            {"timestamp": now + 1, "message": "error: undefined reference to gst_pad_new"},
        ])

        result = benv.deliver_result(
            arch="arm64_jp4", build_id=build_id, status="FAILED",
            plugin_id=plugin_id, version=version, usecase_id=usecase_id,
            plugin_name="blur-regions",
            logs={"group-name": group, "stream-name": stream})

        assert result["recorded"] is True
        entry = benv.get_item(plugin_id, version)["artifacts"]["arm64_jp4"]
        assert entry["buildStatus"] == "failed"
        assert "undefined reference" in entry["logTail"]
        assert "s3Key" not in entry and "checksum" not in entry \
            and "signature" not in entry

    def test_duplicate_delivery_is_idempotent_on_build_id(self, benv, monkeypatch):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        invocations = []
        monkeypatch.setattr(
            benv.module, "lambda_client",
            type("Stub", (), {"invoke": staticmethod(
                lambda **kw: invocations.append(kw))})())

        build_id, first = benv.submit_and_succeed(admin, plugin, "x86_64")
        entry_after_first = benv.get_item(
            plugin["plugin_id"], plugin["version"])["artifacts"]["x86_64"]

        duplicate = benv.deliver_result(
            arch="x86_64", build_id=build_id, status="SUCCEEDED",
            plugin_id=plugin["plugin_id"], version=plugin["version"],
            usecase_id=usecase_id, plugin_name="blur-regions")

        assert first["recorded"] is True
        assert duplicate["recorded"] is False
        assert duplicate["reason"] == "already recorded"
        entry_after_dup = benv.get_item(
            plugin["plugin_id"], plugin["version"])["artifacts"]["x86_64"]
        assert entry_after_dup == entry_after_first
        assert len(invocations) == 1  # component packaging triggered once

    def test_fetch_project_events_delegate_to_the_import_result_handler(
            self, benv):
        """dda-plugin-fetch state changes route to
        plugin_importer.handle_fetch_result (async import) instead of
        the per-arch recording; without attribution env vars nothing is
        recorded. The full fetch-result flow is covered in
        test_plugin_importer.py."""
        event = {
            "source": "aws.codebuild",
            "detail-type": "CodeBuild Build State Change",
            "detail": {"build-status": "SUCCEEDED",
                       "project-name": "dda-plugin-fetch",
                       "build-id": "arn:aws:codebuild:us-east-1:1:build/dda-plugin-fetch:x"},
        }
        result = benv.module.handler(event, None)
        assert result == {"recorded": False,
                          "reason": "missing fetch metadata"}


class TestComponentPackagingTrigger:
    """plugin_components.py trigger on settlement with >= 1 success (16.1)."""

    def test_triggers_once_when_all_requested_builds_settle(self, benv, monkeypatch):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        plugin_id, version = plugin["plugin_id"], plugin["version"]
        invocations = []
        monkeypatch.setattr(
            benv.module, "lambda_client",
            type("Stub", (), {"invoke": staticmethod(
                lambda **kw: invocations.append(kw))})())

        benv.post_build(admin, plugin_id, version,
                        {"architectures": ["x86_64", "arm64_jp4"]})
        item = benv.get_item(plugin_id, version)

        # First arch succeeds: not settled yet, no trigger.
        benv.put_promoted_artifact(usecase_id, "x86_64", "blur-regions", b"so")
        r1 = benv.deliver_result(
            arch="x86_64", build_id=item["artifacts"]["x86_64"]["buildId"],
            status="SUCCEEDED", plugin_id=plugin_id, version=version,
            usecase_id=usecase_id, plugin_name="blur-regions")
        assert r1["component_packaging_triggered"] is False
        assert invocations == []

        # Second arch fails: settled with one success -> single trigger.
        r2 = benv.deliver_result(
            arch="arm64_jp4", build_id=item["artifacts"]["arm64_jp4"]["buildId"],
            status="FAILED", plugin_id=plugin_id, version=version,
            usecase_id=usecase_id, plugin_name="blur-regions")
        assert r2["component_packaging_triggered"] is True
        assert len(invocations) == 1
        payload = json.loads(invocations[0]["Payload"])
        assert payload["plugin_id"] == plugin_id
        assert payload["version"] == version
        assert invocations[0]["FunctionName"] == "test-plugin-components"

    def test_all_failed_never_triggers(self, benv, monkeypatch):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        plugin_id, version = plugin["plugin_id"], plugin["version"]
        invocations = []
        monkeypatch.setattr(
            benv.module, "lambda_client",
            type("Stub", (), {"invoke": staticmethod(
                lambda **kw: invocations.append(kw))})())

        benv.post_build(admin, plugin_id, version,
                        {"architectures": ["x86_64"]})
        item = benv.get_item(plugin_id, version)
        result = benv.deliver_result(
            arch="x86_64", build_id=item["artifacts"]["x86_64"]["buildId"],
            status="FAILED", plugin_id=plugin_id, version=version,
            usecase_id=usecase_id, plugin_name="blur-regions")

        assert result["component_packaging_triggered"] is False
        assert invocations == []

    def test_missing_components_function_never_fails_the_build(self, benv):
        """plugin_components.py does not exist yet (task 6.3): the real
        Lambda invoke raises ResourceNotFoundException, which must be
        tolerated (auto-packaging failure never fails the build)."""
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)

        _, result = benv.submit_and_succeed(admin, plugin, "x86_64")

        assert result["recorded"] is True
        assert result["buildStatus"] == "succeeded"
        assert result["component_packaging_triggered"] is False


class TestPrebuiltUpload:
    """Prebuilt binary path: checksummed and signed identically (3.6)."""

    def test_inline_prebuilt_binary_is_stored_signed_with_provenance(self, benv):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        data = b"\x7fELF prebuilt jetson binary"

        status, body = benv.post_build(
            admin, plugin["plugin_id"], plugin["version"],
            {"architectures": [],
             "prebuilt": {"arm64_jp5": {
                 "content_base64": base64.b64encode(data).decode()}}})

        assert status == 202, body
        item = benv.get_item(plugin["plugin_id"], plugin["version"])
        entry = item["artifacts"]["arm64_jp5"]
        so_key = benv.versioned_library_key(
            usecase_id, "arm64_jp5", plugin["plugin_id"], plugin["version"],
            "blur-regions")
        assert entry["buildStatus"] == "succeeded"
        assert entry["prebuilt"] is True
        assert entry["s3Key"] == so_key
        assert entry["checksum"] == hashlib.sha256(data).hexdigest()
        assert item["provenance"]["prebuilt"] is True
        # Artifact + detached signature promoted to the Plugin_Library.
        stored = benv.s3.get_object(Bucket=benv.bucket, Key=so_key)["Body"].read()
        assert stored == data
        sig = benv.s3.get_object(Bucket=benv.bucket,
                                 Key=so_key + ".sig")["Body"].read()
        assert sig == base64.b64decode(entry["signature"])

    def test_prebuilt_from_source_tree_and_immediate_settlement(self, benv, monkeypatch):
        """A prebuilt-only submission settles immediately and triggers
        component packaging (16.1)."""
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        invocations = []
        monkeypatch.setattr(
            benv.module, "lambda_client",
            type("Stub", (), {"invoke": staticmethod(
                lambda **kw: invocations.append(kw))})())
        data = b"prebuilt so from imported repo"
        item = benv.get_item(plugin["plugin_id"], plugin["version"])
        benv.s3.put_object(Bucket=benv.bucket,
                           Key=item["source_s3_prefix"] + "prebuilt/x86_64/p.so",
                           Body=data)

        status, body = benv.post_build(
            admin, plugin["plugin_id"], plugin["version"],
            {"prebuilt": {"x86_64": {"source_key": "prebuilt/x86_64/p.so"}}})

        assert status == 202, body
        assert body["settled"] is True
        assert body["builds"]["x86_64"]["checksum"] == \
            hashlib.sha256(data).hexdigest()
        assert len(invocations) == 1

    def test_prebuilt_rejects_missing_source_key(self, benv):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)

        status, body = benv.post_build(
            admin, plugin["plugin_id"], plugin["version"],
            {"prebuilt": {"x86_64": {"source_key": "no/such/file.so"}}})

        assert status == 400
        assert body["error"]["code"] == "INVALID_PREBUILT"


class TestBuildStatusEndpoint:
    """GET /plugins/{id}/versions/{v}/builds for the UI (3.5)."""

    def test_viewer_reads_per_arch_status(self, benv):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        benv.submit_and_succeed(admin, plugin, "x86_64")
        viewer = benv.make_user()
        benv.assign_role(viewer, usecase_id, "Viewer")

        status, body = benv.get_builds(viewer, plugin["plugin_id"],
                                       plugin["version"])

        assert status == 200
        assert body["builds"]["x86_64"]["buildStatus"] == "succeeded"
        assert body["builds"]["x86_64"]["checksum"]
        assert body["settled"] is True

    def test_outsider_cannot_submit_builds(self, benv):
        """A user with no role in the Use_Case cannot submit builds."""
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        outsider = benv.make_user()

        status, body = benv.post_build(outsider, plugin["plugin_id"],
                                       plugin["version"],
                                       {"architectures": ["x86_64"]})

        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"


class _RecordingCodeBuild:
    """Proxy over the real (moto) CodeBuild client that records every
    StartBuild call's kwargs."""

    def __init__(self, real):
        self._real = real
        self.calls = []

    def start_build(self, **kwargs):
        self.calls.append(kwargs)
        return self._real.start_build(**kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestPluginTargetsPassThrough:
    """A plugin-set import's recorded selected_plugins are passed to
    CodeBuild as the PLUGIN_TARGETS env override (custom-node-designer
    import selection enhancement)."""

    def _env_overrides(self, call):
        return {var["name"]: var["value"]
                for var in call["environmentVariablesOverride"]}

    def test_plugin_targets_value_joins_the_selection(self, benv):
        mod = benv.module
        assert mod.plugin_targets_value(
            {"selected_plugins": ["rtp", "udp", "v4l2"]}) == "rtp,udp,v4l2"
        assert mod.plugin_targets_value({"selected_plugins": []}) == ""
        assert mod.plugin_targets_value({}) == ""

    def test_selected_plugins_pass_through_as_plugin_targets(
            self, benv, monkeypatch):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        benv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin["plugin_id"],
                 "version": plugin["version"]},
            UpdateExpression="SET selected_plugins = :sp",
            ExpressionAttributeValues={":sp": ["rtp", "udp"]},
        )
        recorder = _RecordingCodeBuild(benv.module.codebuild)
        monkeypatch.setattr(benv.module, "codebuild", recorder)

        status, _ = benv.post_build(admin, plugin["plugin_id"],
                                    plugin["version"],
                                    {"architectures": ["x86_64",
                                                       "arm64_jp5"]})

        assert status == 202
        assert len(recorder.calls) == 2
        for call in recorder.calls:
            env = self._env_overrides(call)
            # The selection reaches every per-arch StartBuild as the
            # comma-separated PLUGIN_TARGETS override the build image
            # entrypoint consumes.
            assert env["PLUGIN_TARGETS"] == "rtp,udp"

    def test_plugin_targets_is_empty_without_a_selection(
            self, benv, monkeypatch):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        recorder = _RecordingCodeBuild(benv.module.codebuild)
        monkeypatch.setattr(benv.module, "codebuild", recorder)

        status, _ = benv.post_build(admin, plugin["plugin_id"],
                                    plugin["version"],
                                    {"architectures": ["x86_64"]})

        assert status == 202
        env = self._env_overrides(recorder.calls[0])
        # No selection (scaffold/generated/single-plugin records):
        # PLUGIN_TARGETS is passed empty, meaning "build everything".
        assert env["PLUGIN_TARGETS"] == ""


class TestSelectedPluginsPassThrough:
    """A provenance-recorded plugin selection (import-time
    selected_plugins on POST /plugins/import) is passed to CodeBuild as
    the SELECTED_PLUGINS env override so the build entrypoint can
    meson-enable only those plugins."""

    def _env_overrides(self, call):
        return {var["name"]: var["value"]
                for var in call["environmentVariablesOverride"]}

    def test_selected_plugins_value_prefers_provenance(self, benv):
        mod = benv.module
        assert mod.selected_plugins_value(
            {"provenance": {"selectedPlugins": ["rtp", "udp"]}}) == "rtp,udp"
        # Fallback to the record-level field (select-plugins endpoint
        # writes both).
        assert mod.selected_plugins_value(
            {"selected_plugins": ["jpeg"]}) == "jpeg"
        assert mod.selected_plugins_value({"provenance": {}}) == ""
        assert mod.selected_plugins_value({}) == ""

    def test_provenance_selection_passes_through_as_selected_plugins(
            self, benv, monkeypatch):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        benv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin["plugin_id"],
                 "version": plugin["version"]},
            UpdateExpression="SET provenance.selectedPlugins = :sp",
            ExpressionAttributeValues={":sp": ["rtp", "udp"]},
        )
        recorder = _RecordingCodeBuild(benv.module.codebuild)
        monkeypatch.setattr(benv.module, "codebuild", recorder)

        status, _ = benv.post_build(admin, plugin["plugin_id"],
                                    plugin["version"],
                                    {"architectures": ["x86_64",
                                                       "arm64_jp5"]})

        assert status == 202
        assert len(recorder.calls) == 2
        for call in recorder.calls:
            env = self._env_overrides(call)
            # Comma-separated selection on every per-arch StartBuild.
            assert env["SELECTED_PLUGINS"] == "rtp,udp"

    def test_selected_plugins_is_absent_without_a_selection(
            self, benv, monkeypatch):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        recorder = _RecordingCodeBuild(benv.module.codebuild)
        monkeypatch.setattr(benv.module, "codebuild", recorder)

        status, _ = benv.post_build(admin, plugin["plugin_id"],
                                    plugin["version"],
                                    {"architectures": ["x86_64"]})

        assert status == 202
        env = self._env_overrides(recorder.calls[0])
        # No selection: the env var is not added at all (whole-module
        # builds are indistinguishable from today's).
        assert "SELECTED_PLUGINS" not in env


class TestErrorExcerpt:
    """extract_error_excerpt: the failed-build logTail shows the actual
    error, not the post-failure upload boilerplate (3.4)."""

    def _lines(self, benv, *sections):
        return benv.module.extract_error_excerpt(
            [line for section in sections for line in section])

    def test_cuts_post_failure_boilerplate(self, benv):
        build = [
            "[dda-plugin-build] meson setup /tmp/build .",
            "Dependency gstreamer-1.0 found: NO found 1.14.5 but need: '>= 1.19.0'",
            "meson.build:246:10: ERROR: Neither a subproject directory nor a gstreamer.wrap file was found.",
            "[Container] Phase complete: BUILD State: FAILED",
            "[Container] Phase context status code: COMMAND_EXECUTION_ERROR Message: exit status 1",
        ]
        boilerplate = [
            "[Container] Expanding base directory path: .",
            "[Container] Assembling file list",
            "[Container] Expanding **/*",
            "[Container] No matching auto discover report paths found",
            "[Container] Phase complete: UPLOAD_ARTIFACTS State: SUCCEEDED",
        ] * 30
        excerpt = self._lines(benv, build, boilerplate)
        assert "ERROR: Neither a subproject directory" in excerpt
        assert "found 1.14.5 but need" in excerpt
        assert "COMMAND_EXECUTION_ERROR" in excerpt
        assert "UPLOAD_ARTIFACTS" not in excerpt

    def test_keeps_context_before_last_error(self, benv):
        filler = [f"[42/630] Compiling C object plugin{i}.c.o"
                  for i in range(200)]
        build = filler + [
            "FAILED: gst/rtsp/libgstrtsp.so",
            "error: undefined reference to gst_pad_new",
            "ninja: build stopped: subcommand failed.",
            "[Container] Phase complete: BUILD State: FAILED",
        ]
        excerpt = self._lines(benv, build)
        assert "undefined reference to gst_pad_new" in excerpt
        # A bounded context window, not the whole 200-line compile log.
        assert "plugin0.c.o" not in excerpt
        assert "plugin199.c.o" in excerpt

    def test_falls_back_to_plain_tail_without_error_lines(self, benv):
        lines = [f"line {i}" for i in range(10)]
        assert benv.module.extract_error_excerpt(lines) == "\n".join(lines)

    def test_empty_lines(self, benv):
        assert benv.module.extract_error_excerpt([]) == ""


class TestGstIntrospectionStanza:
    """gstIntrospection stanza recording on the x86_64 artifact entry
    (gst-parameter-prepopulation task 4.4, Requirements 1.4, 1.6).

    The SUCCEEDED path validates the report the build uploaded next to
    the promoted .so ({plugin}.so.gstinspect.json) and records either a
    captured stanza {status, s3Key, gstVersion, capturedAt} or a failed
    stanza {status, message}; recording is best-effort and never alters
    the build's succeeded status (1.4)."""

    #: A valid captured Introspection_Report (gst_properties shape v1).
    CAPTURED_REPORT = {
        "reportVersion": 1,
        "status": "captured",
        "gstVersion": "1.20.3",
        "capturedAt": "2025-01-15T10:30:00Z",
        "elements": [{
            "factory": "blurregions",
            "elementGType": "GstBlurRegions",
            "properties": [{
                "name": "strength",
                "gtype": "gint",
                "owner": "GstBlurRegions",
                "writable": True,
                "blurb": "Blur strength",
                "default": 5,
                "min": 0,
                "max": 100,
            }],
        }],
    }

    def _succeed_with_report(self, benv, report_body=None, arch="x86_64"):
        """POST /build for one arch, stage the promoted .so (and the
        introspection report when given), deliver SUCCEEDED, and return
        (result, artifact entry, report key)."""
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        plugin_id, version = plugin["plugin_id"], plugin["version"]
        status, body = benv.post_build(admin, plugin_id, version,
                                       {"architectures": [arch]})
        assert status == 202, body
        build_id = benv.get_item(plugin_id, version)["artifacts"][arch]["buildId"]
        so_key = benv.put_promoted_artifact(usecase_id, arch, "blur-regions",
                                            b"\x7fELF-shared-object")
        report_key = so_key + ".gstinspect.json"
        if report_body is not None:
            benv.s3.put_object(Bucket=benv.bucket, Key=report_key,
                               Body=report_body)
        result = benv.deliver_result(
            arch=arch, build_id=build_id, status="SUCCEEDED",
            plugin_id=plugin_id, version=version,
            usecase_id=usecase_id, plugin_name="blur-regions")
        entry = benv.get_item(plugin_id, version)["artifacts"][arch]
        return result, entry, report_key

    def _assert_build_untouched(self, result, entry):
        """The SUCCEEDED handling is unaffected by stanza recording (1.4):
        status, artifact key, checksum, and signature are all recorded."""
        assert result["recorded"] is True
        assert entry["buildStatus"] == "succeeded"
        assert entry["s3Key"] and entry["checksum"] and entry["signature"]

    def test_captured_report_records_captured_stanza(self, benv):
        """A valid captured report yields {status: captured, s3Key,
        gstVersion, capturedAt} on the x86_64 entry."""
        result, entry, report_key = self._succeed_with_report(
            benv, json.dumps(self.CAPTURED_REPORT).encode())

        assert entry["gstIntrospection"] == {
            "status": "captured",
            "s3Key": report_key,
            "gstVersion": "1.20.3",
            "capturedAt": "2025-01-15T10:30:00Z",
        }
        self._assert_build_untouched(result, entry)

    def test_failed_report_carries_its_message(self, benv):
        """A report that itself recorded a capture failure becomes a
        failed stanza carrying the report's message."""
        failed_report = {
            "reportVersion": 1,
            "status": "failed",
            "message": "Gst.init failed: no registry",
            "elements": [],
        }
        result, entry, _ = self._succeed_with_report(
            benv, json.dumps(failed_report).encode())

        stanza = entry["gstIntrospection"]
        assert stanza["status"] == "failed"
        assert stanza["message"] == "Gst.init failed: no registry"
        self._assert_build_untouched(result, entry)

    def test_missing_report_object_records_failed_stanza(self, benv):
        """No uploaded report (e.g. the build image predates the
        introspection step) -> failed stanza with a diagnostic."""
        result, entry, _ = self._succeed_with_report(benv, report_body=None)

        stanza = entry["gstIntrospection"]
        assert stanza["status"] == "failed"
        assert "No introspection report" in stanza["message"]
        self._assert_build_untouched(result, entry)

    def test_oversized_report_records_failed_stanza(self, benv):
        """A report over the 256 KiB cap is rejected with a failed
        stanza naming the cap."""
        oversized = b"x" * (benv.module.GST_REPORT_MAX_BYTES + 1)
        result, entry, _ = self._succeed_with_report(benv, oversized)

        stanza = entry["gstIntrospection"]
        assert stanza["status"] == "failed"
        assert "size cap" in stanza["message"]
        self._assert_build_untouched(result, entry)

    # -------- extended (pads-bearing) report shape
    # (port-guidance-and-pad-prepopulation task 4.3, Requirement 3.3)

    def _pads_bearing_report(self, pad_count, caps_len):
        """CAPTURED_REPORT extended with `pad_count` valid always-pads
        whose caps strings are `caps_len` characters each (the extended
        version-1 report shape of port-guidance-and-pad-prepopulation)."""
        report = json.loads(json.dumps(self.CAPTURED_REPORT))
        element = report["elements"][0]
        prefix = "video/x-raw, format=(string)"
        element["pads"] = [{
            "name": f"{'sink' if i % 2 == 0 else 'src'}_{i}",
            "direction": "sink" if i % 2 == 0 else "src",
            "presence": "always",
            "caps": (prefix + "R" * caps_len)[:caps_len],
            "capsTruncated": False,
        } for i in range(pad_count)]
        element["padsError"] = None
        return report

    def test_pads_bearing_report_records_captured_stanza(self, benv):
        """The unchanged stanza code accepts the extended report shape:
        a valid captured report carrying pad data under the size cap
        records the ordinary captured stanza."""
        report = self._pads_bearing_report(pad_count=2, caps_len=64)
        result, entry, report_key = self._succeed_with_report(
            benv, json.dumps(report).encode())

        assert entry["gstIntrospection"] == {
            "status": "captured",
            "s3Key": report_key,
            "gstVersion": "1.20.3",
            "capturedAt": "2025-01-15T10:30:00Z",
        }
        self._assert_build_untouched(result, entry)

    def test_oversized_pads_bearing_report_records_failed_stanza(self, benv):
        """A pads-bearing report over the 256 KiB cap — likelier now
        that pad data (caps up to 4096 chars per pad) rides along —
        yields the failed stanza with the size-cap diagnostic while the
        build status stays succeeded (3.3)."""
        report = self._pads_bearing_report(pad_count=80, caps_len=4000)
        body = json.dumps(report).encode()
        assert len(body) > benv.module.GST_REPORT_MAX_BYTES
        # The document itself is a valid extended-shape report — only
        # the size cap fails it, not pad validation.
        benv.module.parse_report(json.loads(body.decode()))

        result, entry, _ = self._succeed_with_report(benv, body)

        stanza = entry["gstIntrospection"]
        assert stanza["status"] == "failed"
        assert "size cap" in stanza["message"]
        self._assert_build_untouched(result, entry)

    def test_invalid_json_records_failed_stanza(self, benv):
        """A report object that is not JSON at all -> failed stanza."""
        result, entry, _ = self._succeed_with_report(benv, b"not json {{{")

        stanza = entry["gstIntrospection"]
        assert stanza["status"] == "failed"
        assert "not valid JSON" in stanza["message"]
        self._assert_build_untouched(result, entry)

    def test_malformed_report_shape_records_failed_stanza(self, benv):
        """Valid JSON that fails gst_properties.parse_report (wrong
        reportVersion here) -> failed stanza with the shape diagnostic."""
        bad_shape = {"reportVersion": 99, "status": "captured"}
        result, entry, _ = self._succeed_with_report(
            benv, json.dumps(bad_shape).encode())

        stanza = entry["gstIntrospection"]
        assert stanza["status"] == "failed"
        assert "malformed" in stanza["message"]
        self._assert_build_untouched(result, entry)

    def test_non_x86_64_entry_gets_no_stanza(self, benv):
        """Property_Introspection is x86_64-only: another arch's entry
        never carries a gstIntrospection stanza even when a report
        object exists next to its artifact."""
        result, entry, _ = self._succeed_with_report(
            benv, json.dumps(self.CAPTURED_REPORT).encode(), arch="arm64_jp5")

        assert "gstIntrospection" not in entry
        self._assert_build_untouched(result, entry)
