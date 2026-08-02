"""
Unit tests for plugin_records.py (custom-node-designer task 3.1).

Covers Plugin_Record creation and versioning (9.1, 9.13, 10.1, 10.5),
lifecycle transitions with guards (9.4, 9.5, 9.9, 9.10, 9.12), and the
security review endpoints with provenance display, source inspection,
and AuditLog recording (9.3, 10.2, 10.3, 15.6).

Runs against the moto-backed stack from conftest.py, exercising the
real RBAC / audit / persistence code paths.
"""
import json
import uuid

import pytest
from boto3.dynamodb.conditions import Key

from conftest import TEST_ENV


class PluginRecordsEnv:
    """Facade for invoking the Plugin_Record API in tests."""

    def __init__(self, stack):
        self.stack = stack
        self.module = stack.plugin_records
        self.s3 = stack.s3
        self.bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]

    # ------------------------------------------------------------- setup
    def create_usecase(self, name="Plugin Test Use Case"):
        usecase_id = f"uc-{uuid.uuid4()}"
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": usecase_id,
            "name": name,
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

    def seed_artifact(self, plugin_id, version, arch="x86_64",
                      build_status="succeeded", checksum="ab" * 32,
                      signature="sig-bytes"):
        """Record a per-arch Plugin_Artifact entry directly on the item."""
        self.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin_id, "version": version},
            UpdateExpression="SET artifacts.#a = :entry",
            ExpressionAttributeNames={"#a": arch},
            ExpressionAttributeValues={":entry": {
                "s3Key": f"workflow-plugins/custom/uc/{arch}/p.so",
                "checksum": checksum,
                "signature": signature,
                "buildStatus": build_status,
                "logTail": "" if build_status == "succeeded" else "error: boom",
            }},
        )

    def audit_entries(self, action):
        response = self.stack.tables.audit_log.scan()
        return [i for i in response["Items"] if i["action"] == action]

    # ----------------------------------------------------------- invoke
    def invoke(self, method, resource, user, plugin_id=None, version=None,
               body=None, query=None):
        path_params = {}
        if plugin_id is not None:
            path_params["id"] = plugin_id
        if version is not None:
            path_params["v"] = str(version)
        event = {
            "httpMethod": method,
            "resource": resource,
            "path": resource.replace("{id}", plugin_id or "").replace("{v}", str(version or "")),
            "pathParameters": path_params or None,
            "queryStringParameters": query,
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
        response = self.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    # ------------------------------------------------------ conveniences
    def create_plugin(self, user, usecase_id, name="blur-regions",
                      kind="scaffold", **extra):
        body = {"usecase_id": usecase_id, "name": name, "kind": kind}
        body.update(extra)
        return self.invoke("POST", "/plugins", user, body=body)

    def promote(self, user, plugin_id, version):
        return self.invoke("POST", "/plugins/{id}/versions/{v}/promote",
                           user, plugin_id, version)

    def demote(self, user, plugin_id, version):
        return self.invoke("POST", "/plugins/{id}/versions/{v}/demote",
                           user, plugin_id, version)

    def review(self, user, plugin_id, version, decision, **extra):
        body = {"decision": decision}
        body.update(extra)
        return self.invoke("POST", "/plugins/{id}/versions/{v}/review",
                           user, plugin_id, version, body=body)


@pytest.fixture
def penv(aws_stack):
    return PluginRecordsEnv(aws_stack)


@pytest.fixture
def admin_setup(penv):
    """A Use_Case with a UseCaseAdmin assigned to it."""
    usecase_id = penv.create_usecase()
    admin = penv.make_user(role="Viewer")
    penv.assign_role(admin, usecase_id, "UseCaseAdmin")
    return usecase_id, admin


# ----------------------------------------------------- creation (9.1, 10.1)

class TestCreation:
    def test_create_starts_in_dev_with_pending_review(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        status, body = penv.create_plugin(admin, usecase_id)
        assert status == 201
        plugin = body["plugin"]
        assert plugin["lifecycle_state"] == "dev"
        assert plugin["review"]["decision"] == "pending"
        assert plugin["version"] == 1
        assert plugin["source_s3_prefix"] == (
            f"plugin-sources/{usecase_id}/{plugin['plugin_id']}/1/"
        )

    def test_create_records_provenance(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        status, body = penv.create_plugin(
            admin, usecase_id, kind="imported",
            provenance={
                "repoUrl": "https://example.com/repo.git",
                "revision": "abc123",
                "classification": "unclassified",
            })
        assert status == 201
        prov = body["plugin"]["provenance"]
        assert prov["repoUrl"] == "https://example.com/repo.git"
        assert prov["revision"] == "abc123"
        assert prov["classification"] == "unclassified"
        assert prov["createdBy"] == admin["user_id"]
        assert prov["createdAt"] > 0

    def test_create_rejects_invalid_kind(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        status, body = penv.create_plugin(admin, usecase_id, kind="bogus")
        assert status == 400
        assert body["error"]["code"] == "INVALID_KIND"

    def test_create_denied_for_non_admin(self, penv):
        usecase_id = penv.create_usecase()
        operator = penv.make_user(role="Viewer")
        penv.assign_role(operator, usecase_id, "Operator")
        status, body = penv.create_plugin(operator, usecase_id)
        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"


# --------------------------------------------- new versions (9.13, 10.5)

class TestVersioning:
    def test_new_version_resets_state_and_review_independently(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id)
        plugin_id = body["plugin"]["plugin_id"]

        # Drive v1 to prod with an approved review
        penv.seed_artifact(plugin_id, 1)
        assert penv.promote(admin, plugin_id, 1)[0] == 200
        portal_admin = penv.make_user(role="PortalAdmin")
        assert penv.review(portal_admin, plugin_id, 1, "approved")[0] == 200
        assert penv.promote(admin, plugin_id, 1)[0] == 200

        # New version from changed source
        status, body = penv.invoke(
            "PUT", "/plugins/{id}", admin, plugin_id,
            body={"new_version": True})
        assert status == 201
        v2 = body["plugin"]
        assert v2["version"] == 2
        assert v2["lifecycle_state"] == "dev"
        assert v2["review"]["decision"] == "pending"
        assert v2["artifacts"] == {}

        # Prior version untouched
        status, body = penv.invoke(
            "GET", "/plugins/{id}/versions/{v}", admin, plugin_id, 1)
        assert status == 200
        assert body["plugin"]["lifecycle_state"] == "prod"
        assert body["plugin"]["review"]["decision"] == "approved"


# ------------------------------------- lifecycle guards (9.4, 9.5, 9.9, 9.10)

class TestLifecycleTransitions:
    def test_promote_dev_to_test_without_build_is_409(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id)
        plugin_id = body["plugin"]["plugin_id"]

        status, body = penv.promote(admin, plugin_id, 1)
        assert status == 409
        assert body["error"]["code"] == "PLUGIN_BUILD_REQUIRED"
        # Rejection identifies the missing build (9.5)
        assert body["error"]["details"]["missing"] == "successfully built Plugin_Artifact"

    def test_failed_build_does_not_satisfy_promotion_guard(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id)
        plugin_id = body["plugin"]["plugin_id"]
        penv.seed_artifact(plugin_id, 1, build_status="failed")

        status, body = penv.promote(admin, plugin_id, 1)
        assert status == 409
        assert body["error"]["code"] == "PLUGIN_BUILD_REQUIRED"

    def test_promote_dev_to_test_with_build_succeeds(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id)
        plugin_id = body["plugin"]["plugin_id"]
        penv.seed_artifact(plugin_id, 1)

        status, body = penv.promote(admin, plugin_id, 1)
        assert status == 200
        assert body["plugin"]["lifecycle_state"] == "test"

    def test_promote_test_to_prod_without_approval_is_409(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id)
        plugin_id = body["plugin"]["plugin_id"]
        penv.seed_artifact(plugin_id, 1)
        penv.promote(admin, plugin_id, 1)

        status, body = penv.promote(admin, plugin_id, 1)
        assert status == 409
        assert body["error"]["code"] == "SECURITY_REVIEW_REQUIRED"
        # Rejection identifies the missing approval (9.10)
        assert body["error"]["details"]["missing"] == "approved security review"
        assert body["error"]["details"]["review_decision"] == "pending"

    def test_promote_test_to_prod_after_approval_succeeds(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id)
        plugin_id = body["plugin"]["plugin_id"]
        penv.seed_artifact(plugin_id, 1)
        penv.promote(admin, plugin_id, 1)
        portal_admin = penv.make_user(role="PortalAdmin")
        penv.review(portal_admin, plugin_id, 1, "approved")

        status, body = penv.promote(admin, plugin_id, 1)
        assert status == 200
        assert body["plugin"]["lifecycle_state"] == "prod"

    def test_promote_from_prod_is_invalid(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id)
        plugin_id = body["plugin"]["plugin_id"]
        penv.seed_artifact(plugin_id, 1)
        penv.promote(admin, plugin_id, 1)
        portal_admin = penv.make_user(role="PortalAdmin")
        penv.review(portal_admin, plugin_id, 1, "approved")
        penv.promote(admin, plugin_id, 1)

        status, body = penv.promote(admin, plugin_id, 1)
        assert status == 409
        assert body["error"]["code"] == "INVALID_LIFECYCLE_TRANSITION"

    def test_demotion_always_succeeds(self, penv, admin_setup):
        """prod->test and test->dev demote without guards (9.12)."""
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id)
        plugin_id = body["plugin"]["plugin_id"]
        penv.seed_artifact(plugin_id, 1)
        penv.promote(admin, plugin_id, 1)
        portal_admin = penv.make_user(role="PortalAdmin")
        penv.review(portal_admin, plugin_id, 1, "approved")
        penv.promote(admin, plugin_id, 1)

        status, body = penv.demote(admin, plugin_id, 1)
        assert status == 200
        assert body["plugin"]["lifecycle_state"] == "test"

        status, body = penv.demote(admin, plugin_id, 1)
        assert status == 200
        assert body["plugin"]["lifecycle_state"] == "dev"

        # Demotion only changed lifecycle_state; the approval stands
        assert body["plugin"]["review"]["decision"] == "approved"

    def test_demote_from_dev_is_invalid(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id)
        plugin_id = body["plugin"]["plugin_id"]
        status, body = penv.demote(admin, plugin_id, 1)
        assert status == 409
        assert body["error"]["code"] == "INVALID_LIFECYCLE_TRANSITION"


# ------------------------------------ security review (10.2, 10.3, 15.6)

class TestSecurityReview:
    def _pending_record(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(
            admin, usecase_id, kind="imported",
            provenance={
                "repoUrl": "https://gitlab.freedesktop.org/gstreamer/gst-plugins-bad.git",
                "revision": "1.20",
                "importedBy": admin["user_id"],
                "importedAt": 1700000000000,
                "classification": "bad",
            })
        plugin_id = body["plugin"]["plugin_id"]
        penv.seed_artifact(plugin_id, 1, arch="x86_64", checksum="aa" * 32,
                           signature="sig-x86")
        penv.seed_artifact(plugin_id, 1, arch="arm64_jp5", checksum="bb" * 32,
                           signature="sig-jp5")
        return usecase_id, admin, plugin_id

    def test_pending_display_shows_provenance_checksums_signatures(self, penv, admin_setup):
        usecase_id, admin, plugin_id = self._pending_record(penv, admin_setup)
        portal_admin = penv.make_user(role="PortalAdmin")

        status, body = penv.invoke(
            "GET", "/plugins/{id}/versions/{v}", portal_admin, plugin_id, 1)
        assert status == 200
        plugin = body["plugin"]
        # Full provenance incl. classification (10.2, 15.6)
        assert plugin["provenance"]["repoUrl"].endswith("gst-plugins-bad.git")
        assert plugin["provenance"]["revision"] == "1.20"
        assert plugin["provenance"]["importedBy"]
        assert plugin["provenance"]["importedAt"]
        assert plugin["provenance"]["classification"] == "bad"
        # Per-arch checksums and signatures (10.2)
        assert plugin["artifacts"]["x86_64"]["checksum"] == "aa" * 32
        assert plugin["artifacts"]["x86_64"]["signature"] == "sig-x86"
        assert plugin["artifacts"]["arm64_jp5"]["checksum"] == "bb" * 32
        assert plugin["artifacts"]["arm64_jp5"]["signature"] == "sig-jp5"
        assert plugin["review"]["decision"] == "pending"

    def test_review_queue_lists_pending_records(self, penv, admin_setup):
        usecase_id, admin, plugin_id = self._pending_record(penv, admin_setup)
        portal_admin = penv.make_user(role="PortalAdmin")
        penv.assign_role(portal_admin, "global", "PortalAdmin")

        status, body = penv.invoke(
            "GET", "/plugins", portal_admin,
            query={"usecase_id": usecase_id, "review": "pending"})
        assert status == 200
        ids = [p["plugin_id"] for p in body["plugins"]]
        assert plugin_id in ids

    def test_source_inspection(self, penv, admin_setup):
        usecase_id, admin, plugin_id = self._pending_record(penv, admin_setup)
        prefix = f"plugin-sources/{usecase_id}/{plugin_id}/1/"
        penv.s3.put_object(Bucket=penv.bucket, Key=prefix + "meson.build",
                           Body=b"project('blur')")
        penv.s3.put_object(Bucket=penv.bucket, Key=prefix + "src/hook.py",
                           Body=b"def process_frame(frame, params):\n    return frame\n")
        portal_admin = penv.make_user(role="PortalAdmin")

        status, body = penv.invoke(
            "GET", "/plugins/{id}/versions/{v}/source", portal_admin, plugin_id, 1)
        assert status == 200
        files = {f["file"] for f in body["files"]}
        assert files == {"meson.build", "src/hook.py"}

        status, body = penv.invoke(
            "GET", "/plugins/{id}/versions/{v}/source", portal_admin, plugin_id, 1,
            query={"file": "src/hook.py"})
        assert status == 200
        assert "process_frame" in body["content"]

        # Path traversal is rejected
        status, body = penv.invoke(
            "GET", "/plugins/{id}/versions/{v}/source", portal_admin, plugin_id, 1,
            query={"file": "../../../etc/passwd"})
        assert status == 400

    def test_approve_records_decision_reviewer_timestamp_in_audit_log(self, penv, admin_setup):
        usecase_id, admin, plugin_id = self._pending_record(penv, admin_setup)
        portal_admin = penv.make_user(role="PortalAdmin")

        status, body = penv.review(portal_admin, plugin_id, 1, "approved")
        assert status == 200
        review = body["plugin"]["review"]
        assert review["decision"] == "approved"
        assert review["reviewer"] == portal_admin["user_id"]
        assert review["reviewedAt"] > 0

        # Requirement 10.3: decision + acting PortalAdmin + timestamp in
        # the existing AuditLog table
        entries = [e for e in penv.audit_entries("security_review_approved")
                   if e["resource_id"] == plugin_id]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["user_id"] == portal_admin["user_id"]
        assert entry["timestamp"] > 0
        assert entry["details"]["decision"] == "approved"

    def test_reject_recorded(self, penv, admin_setup):
        usecase_id, admin, plugin_id = self._pending_record(penv, admin_setup)
        portal_admin = penv.make_user(role="PortalAdmin")

        status, body = penv.review(portal_admin, plugin_id, 1, "rejected",
                                   notes="unvetted dependency")
        assert status == 200
        assert body["plugin"]["review"]["decision"] == "rejected"
        entries = [e for e in penv.audit_entries("security_review_rejected")
                   if e["resource_id"] == plugin_id]
        assert len(entries) == 1

    def test_review_denied_for_usecase_admin(self, penv, admin_setup):
        usecase_id, admin, plugin_id = self._pending_record(penv, admin_setup)
        status, body = penv.review(admin, plugin_id, 1, "approved")
        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"

    def test_invalid_decision_rejected(self, penv, admin_setup):
        usecase_id, admin, plugin_id = self._pending_record(penv, admin_setup)
        portal_admin = penv.make_user(role="PortalAdmin")
        status, body = penv.review(portal_admin, plugin_id, 1, "maybe")
        assert status == 400
        assert body["error"]["code"] == "INVALID_REVIEW_DECISION"


# ----------------------------------------------------------- read views

class TestReadViews:
    def test_get_plugin_returns_latest_and_history(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id)
        plugin_id = body["plugin"]["plugin_id"]
        penv.invoke("PUT", "/plugins/{id}", admin, plugin_id,
                    body={"new_version": True})

        status, body = penv.invoke("GET", "/plugins/{id}", admin, plugin_id)
        assert status == 200
        assert body["plugin"]["version"] == 2
        assert [v["version"] for v in body["versions"]] == [2, 1]

    def test_list_scoped_by_usecase(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id)
        plugin_id = body["plugin"]["plugin_id"]

        status, body = penv.invoke("GET", "/plugins", admin,
                                   query={"usecase_id": usecase_id})
        assert status == 200
        assert [p["plugin_id"] for p in body["plugins"]] == [plugin_id]

    def test_unknown_plugin_is_404(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        status, body = penv.invoke("GET", "/plugins/{id}", admin, "nonexistent")
        assert status == 404
        assert body["error"]["code"] == "PLUGIN_NOT_FOUND"


# ------------------------------------------- scaffold create path (task 12.1)

def make_scaffold_declaration(**overrides):
    """A valid Custom_Node_Type declaration for the create-wizard path."""
    declaration = {
        "typeId": "custom.blur_regions",
        "displayName": "Blur Regions",
        "category": "preprocessing",
        "inputs": [{"name": "in", "portType": "VideoFrames"}],
        "outputs": [{"name": "out", "portType": "VideoFrames"}],
        "parameters": [{
            "name": "radius",
            "paramType": "int",
            "required": True,
            "default": 5,
            "description": "Blur radius in pixels",
            "examples": [8],
        }],
        "mappings": [],
        "architectures": ["x86_64", "arm64_jp5"],
    }
    declaration.update(overrides)
    return declaration


class TestScaffoldCreate:
    """POST /plugins with a declaration renders and stores the scaffold
    (Requirements 1.2, 1.5, 1.7)."""

    def _source_keys(self, penv, usecase_id, plugin_id, version=1):
        prefix = f"plugin-sources/{usecase_id}/{plugin_id}/{version}/"
        response = penv.s3.list_objects_v2(Bucket=penv.bucket, Prefix=prefix)
        return {obj["Key"][len(prefix):] for obj in response.get("Contents", [])}

    def test_create_with_declaration_renders_and_stores_scaffold(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        status, body = penv.create_plugin(
            admin, usecase_id, declaration=make_scaffold_declaration())

        assert status == 201
        files = body["files"]
        # Hook + C skeleton + per-arch build configs + README (1.2)
        assert "plugin/frame_processing_hook.py" in files
        assert "builds/x86_64/meson.build" in files
        assert "builds/arm64_jp5/meson.build" in files
        assert "README.md" in files
        # The rendered scaffold lands under plugin-sources for the
        # Plugin_Build_Service (1.5, 1.6).
        plugin_id = body["plugin"]["plugin_id"]
        assert self._source_keys(penv, usecase_id, plugin_id) == set(files)
        # The declaration is recorded as provenance.
        provenance = body["plugin"]["provenance"]
        assert "blur_regions" in provenance["scaffoldDeclaration"]

    def test_invalid_declaration_identifies_field_and_creates_no_record(
            self, penv, admin_setup):
        usecase_id, admin = admin_setup
        status, body = penv.create_plugin(
            admin, usecase_id,
            declaration=make_scaffold_declaration(category="nonsense"))

        # Requirement 1.7: error identifies the failing input, no record.
        assert status == 400
        assert body["error"]["code"] == "INVALID_DECLARATION"
        assert body["error"]["details"]["field"] == "category"

        status, body = penv.invoke("GET", "/plugins", admin,
                                   query={"usecase_id": usecase_id})
        assert body["plugins"] == []

    def test_declaration_rejected_for_non_scaffold_kind(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        status, body = penv.create_plugin(
            admin, usecase_id, kind="imported",
            declaration=make_scaffold_declaration())
        assert status == 400
        assert body["error"]["code"] == "INVALID_DECLARATION"


class TestPutVersionSource:
    """PUT /plugins/{id}/versions/{v}/source persists submitted source
    (original or edited) ahead of a build (Requirement 1.6)."""

    def _scaffold_record(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(
            admin, usecase_id, declaration=make_scaffold_declaration())
        return usecase_id, admin, body["plugin"]["plugin_id"], body["files"]

    def test_edited_source_is_persisted(self, penv, admin_setup):
        usecase_id, admin, plugin_id, files = self._scaffold_record(penv, admin_setup)
        files["plugin/frame_processing_hook.py"] = (
            "def process_frame(frame, params):\n    return frame[::-1]\n")

        status, body = penv.invoke(
            "PUT", "/plugins/{id}/versions/{v}/source", admin, plugin_id, 1,
            body={"files": files})
        assert status == 200
        assert body["count"] == len(files)

        key = f"plugin-sources/{usecase_id}/{plugin_id}/1/plugin/frame_processing_hook.py"
        stored = penv.s3.get_object(Bucket=penv.bucket, Key=key)["Body"].read()
        assert b"frame[::-1]" in stored

    def test_non_buildable_scaffold_source_is_rejected(self, penv, admin_setup):
        usecase_id, admin, plugin_id, files = self._scaffold_record(penv, admin_setup)
        files.pop("plugin/frame_processing_hook.py")

        status, body = penv.invoke(
            "PUT", "/plugins/{id}/versions/{v}/source", admin, plugin_id, 1,
            body={"files": files})
        assert status == 422
        assert body["error"]["code"] == "SCAFFOLD_INVALID"
        assert any("frame_processing_hook" in d
                   for d in body["error"]["details"]["defects"])

    def test_path_traversal_rejected(self, penv, admin_setup):
        usecase_id, admin, plugin_id, files = self._scaffold_record(penv, admin_setup)
        files["../../escape.py"] = "print('nope')"
        status, body = penv.invoke(
            "PUT", "/plugins/{id}/versions/{v}/source", admin, plugin_id, 1,
            body={"files": files})
        assert status == 400
        assert body["error"]["code"] == "INVALID_FILE_PATH"

    def test_requires_manage_permission(self, penv, admin_setup):
        usecase_id, admin, plugin_id, files = self._scaffold_record(penv, admin_setup)
        operator = penv.make_user(role="Viewer")
        penv.assign_role(operator, usecase_id, "Operator")
        status, body = penv.invoke(
            "PUT", "/plugins/{id}/versions/{v}/source", operator, plugin_id, 1,
            body={"files": files})
        assert status == 403


# --------------------------------------------- deletion (DELETE /plugins/{id})

class TestDeletePlugin:
    """DELETE /plugins/{id}: removes every version of the record (bad or
    duplicate imports) with best-effort S3 cleanup, refusing records
    promoted beyond dev with 409 RECORD_IN_USE."""

    def _delete(self, penv, user, plugin_id):
        return penv.invoke("DELETE", "/plugins/{id}", user, plugin_id)

    def _versions_in_table(self, penv, plugin_id):
        response = penv.stack.tables.plugin_records.query(
            KeyConditionExpression=Key("plugin_id").eq(plugin_id))
        return response["Items"]

    def test_delete_removes_every_version_and_audits(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id)
        plugin_id = body["plugin"]["plugin_id"]
        status, _ = penv.invoke("PUT", "/plugins/{id}", admin, plugin_id,
                                body={"new_version": True})
        assert status == 201

        status, body = self._delete(penv, admin, plugin_id)

        assert status == 200
        assert body == {"deleted": True, "plugin_id": plugin_id,
                        "versions": [1, 2]}
        assert self._versions_in_table(penv, plugin_id) == []
        # The deletion is audit-logged.
        entries = penv.audit_entries("delete_plugin_record")
        assert any(e["resource_id"] == plugin_id for e in entries)

    def test_delete_cleans_up_sources_and_promoted_artifacts(
            self, penv, admin_setup):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id)
        plugin = body["plugin"]
        plugin_id = plugin["plugin_id"]
        # Source snapshot under the version's plugin-sources prefix,
        # plus a multi-revision fetch tree.
        prefix = plugin["source_s3_prefix"]
        penv.s3.put_object(Bucket=penv.bucket, Key=f"{prefix}meson.build",
                           Body=b"project('p', 'c')")
        penv.s3.put_object(Bucket=penv.bucket,
                           Key=f"{prefix}rev-1.16/meson.build",
                           Body=b"project('p', 'c')")
        penv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin_id, "version": 1},
            UpdateExpression="SET fetches = :f",
            ExpressionAttributeValues={":f": {
                "1.16": {"revision": "1.16", "status": "succeeded",
                         "source_prefix": f"{prefix}rev-1.16/"},
            }},
        )
        # A promoted Plugin_Library artifact with its detached signature.
        so_key = f"workflow-plugins/custom/{usecase_id}/x86_64/p.so"
        penv.s3.put_object(Bucket=penv.bucket, Key=so_key, Body=b"so")
        penv.s3.put_object(Bucket=penv.bucket, Key=so_key + ".sig", Body=b"s")
        penv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin_id, "version": 1},
            UpdateExpression="SET artifacts.#a = :e",
            ExpressionAttributeNames={"#a": "x86_64"},
            ExpressionAttributeValues={":e": {
                "s3Key": so_key, "buildStatus": "succeeded",
                "checksum": "ab" * 32, "signature": "sig", "logTail": "",
            }},
        )

        status, _ = self._delete(penv, admin, plugin_id)

        assert status == 200
        listed = penv.s3.list_objects_v2(Bucket=penv.bucket, Prefix=prefix)
        assert listed.get("KeyCount", 0) == 0
        for key in (so_key, so_key + ".sig"):
            with pytest.raises(penv.s3.exceptions.ClientError):
                penv.s3.head_object(Bucket=penv.bucket, Key=key)

    def test_s3_cleanup_failure_never_fails_the_delete(
            self, penv, admin_setup, monkeypatch):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id)
        plugin_id = body["plugin"]["plugin_id"]

        def exploding(*args, **kwargs):
            raise RuntimeError("S3 unavailable")

        monkeypatch.setattr(penv.module, "_delete_prefix_objects", exploding)

        status, body = self._delete(penv, admin, plugin_id)

        assert status == 200
        assert body["deleted"] is True
        assert self._versions_in_table(penv, plugin_id) == []

    def test_promoted_version_refuses_deletion(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id)
        plugin_id = body["plugin"]["plugin_id"]
        penv.seed_artifact(plugin_id, 1)
        assert penv.promote(admin, plugin_id, 1)[0] == 200  # dev -> test

        status, body = self._delete(penv, admin, plugin_id)

        assert status == 409
        assert body["error"]["code"] == "RECORD_IN_USE"
        assert body["error"]["details"]["versions"] == [1]
        assert len(self._versions_in_table(penv, plugin_id)) == 1

        # Demoting back to dev unblocks the delete.
        assert penv.demote(admin, plugin_id, 1)[0] == 200
        status, _ = self._delete(penv, admin, plugin_id)
        assert status == 200

    @pytest.mark.parametrize("import_status",
                             ["failed", "fetching", "pending_selection"])
    def test_unsettled_or_failed_imports_are_always_deletable(
            self, penv, admin_setup, import_status):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id, kind="imported")
        plugin_id = body["plugin"]["plugin_id"]
        penv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin_id, "version": 1},
            UpdateExpression="SET import_status = :s",
            ExpressionAttributeValues={":s": import_status},
        )

        status, body = self._delete(penv, admin, plugin_id)

        assert status == 200
        assert body["deleted"] is True
        assert self._versions_in_table(penv, plugin_id) == []

    def test_missing_record_returns_not_found(self, penv, admin_setup):
        _, admin = admin_setup
        status, body = self._delete(penv, admin, "no-such-plugin")
        assert status == 404
        assert body["error"]["code"] == "PLUGIN_NOT_FOUND"

    def test_delete_requires_manage_permission(self, penv, admin_setup):
        usecase_id, admin = admin_setup
        _, body = penv.create_plugin(admin, usecase_id)
        plugin_id = body["plugin"]["plugin_id"]
        operator = penv.make_user(role="Viewer")
        penv.assign_role(operator, usecase_id, "Operator")

        status, body = self._delete(penv, operator, plugin_id)

        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"
        assert len(self._versions_in_table(penv, plugin_id)) == 1
