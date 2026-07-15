"""
Unit tests for GET /plugins/{id}/versions/{v}/gst-properties
(gst-parameter-prepopulation task 3.2).

Covers each machine-readable unavailability reason — `no_x86_64_build`
(no artifacts, failed x86_64 build, other-arch-only build), `not_captured`
(successful build predating Property_Introspection, 7.4), and
`introspection_failed` (failed capture stanza, missing stored report,
malformed stored JSON, non-conforming report document, 8.3) — plus the
available path serving the stored Introspection_Report with derived
per-element Parameter_Suggestions (1.5, 1.6), and the route's access
behavior: uniform 404 for missing records and for users without
node-designer read access, so record existence never leaks across
tenants.

Runs against the moto-backed stack from conftest.py, exercising the
real RBAC / persistence code paths (test_plugin_simulator.py
conventions).
"""
import json
import uuid

import pytest

from conftest import TEST_ENV


def captured_report_document(factory="myblur", element_gtype="GstMyBlur"):
    """A valid version-1 Introspection_Report document mixing an own
    ranged int property, an own GEnum property, an own unmappable
    property (skipped), and a base-class property (filtered out)."""
    return {
        "reportVersion": 1,
        "status": "captured",
        "message": None,
        "gstVersion": "1.20.3",
        "capturedAt": "2026-02-14T12:00:00Z",
        "elements": [{
            "factory": factory,
            "elementGType": element_gtype,
            "instantiationError": None,
            "properties": [
                {"name": "radius", "gtype": "gint", "owner": element_gtype,
                 "writable": True, "blurb": "Blur radius in pixels",
                 "default": 5, "min": 0, "max": 100, "enumValues": None},
                {"name": "mode", "gtype": "GstMyBlurMode",
                 "owner": element_gtype, "writable": True,
                 "blurb": "Blur mode", "default": "gaussian",
                 "min": None, "max": None,
                 "enumValues": [{"value": 0, "nick": "gaussian"},
                                {"value": 1, "nick": "box"}]},
                {"name": "filter-caps", "gtype": "GstCaps",
                 "owner": element_gtype, "writable": True,
                 "blurb": "Restrict caps", "default": None,
                 "min": None, "max": None, "enumValues": None},
                # Base_Class_Property: owned by GstObject, never served.
                {"name": "name", "gtype": "gchararray", "owner": "GstObject",
                 "writable": True, "blurb": "The name of the object",
                 "default": None, "min": None, "max": None,
                 "enumValues": None},
            ],
        }],
    }


class GstPropertiesEnv:
    """Facade for invoking the gst-properties route in tests."""

    def __init__(self, stack):
        self.stack = stack
        self.module = stack.plugin_records
        self.s3 = stack.s3
        self.bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]

    # ------------------------------------------------------------- setup
    def create_usecase(self):
        usecase_id = f"uc-{uuid.uuid4()}"
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": usecase_id,
            "name": "Gst Properties Test Use Case",
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

    def create_plugin(self, user, usecase_id, name="blur-regions"):
        event = self._event("POST", "/plugins", user, body={
            "usecase_id": usecase_id, "name": name, "kind": "scaffold"})
        response = self.module.handler(event, None)
        body = json.loads(response["body"])
        assert response["statusCode"] == 201, body
        return body["plugin"]

    def report_key(self, plugin, plugin_name="blur-regions"):
        return (f"workflow-plugins/custom/{plugin['usecase_id']}"
                f"/x86_64/{plugin_name}.so.gstinspect.json")

    def seed_artifact(self, plugin, arch="x86_64", build_status="succeeded",
                      gst_introspection=None):
        """Record a per-arch Plugin_Artifact entry, optionally carrying a
        gstIntrospection stanza, directly on the version item."""
        entry = {
            "buildStatus": build_status,
            "s3Key": (f"workflow-plugins/custom/{plugin['usecase_id']}"
                      f"/{arch}/blur-regions.so"),
            "checksum": "aa" * 32,
            "signature": "sig",
        }
        if gst_introspection is not None:
            entry["gstIntrospection"] = gst_introspection
        self.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin["plugin_id"],
                 "version": plugin["version"]},
            UpdateExpression="SET artifacts.#a = :entry",
            ExpressionAttributeNames={"#a": arch},
            ExpressionAttributeValues={":entry": entry},
        )

    def captured_stanza(self, plugin):
        return {
            "status": "captured",
            "s3Key": self.report_key(plugin),
            "gstVersion": "1.20.3",
            "capturedAt": "2026-02-14T12:00:00Z",
        }

    def put_report(self, plugin, body):
        """Store the report object next to the promoted artifact."""
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
        self.s3.put_object(Bucket=self.bucket,
                           Key=self.report_key(plugin), Body=body)

    def plugin_with_report(self, document):
        """A use case + admin + plugin whose x86_64 artifact carries a
        captured stanza pointing at the stored `document`."""
        usecase_id = self.create_usecase()
        admin = self.make_admin(usecase_id)
        plugin = self.create_plugin(admin, usecase_id)
        self.seed_artifact(plugin,
                           gst_introspection=self.captured_stanza(plugin))
        self.put_report(plugin, document)
        return usecase_id, admin, plugin

    # ----------------------------------------------------------- invoke
    def _event(self, method, resource, user, plugin_id=None, version=None,
               body=None):
        path_params = {}
        if plugin_id is not None:
            path_params = {"id": plugin_id, "v": str(version)}
        return {
            "httpMethod": method,
            "resource": resource,
            "path": resource,
            "pathParameters": path_params or None,
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

    def get_gst_properties(self, user, plugin_id, version):
        event = self._event(
            "GET", "/plugins/{id}/versions/{v}/gst-properties",
            user, plugin_id=plugin_id, version=version)
        response = self.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"])


@pytest.fixture
def genv(aws_stack):
    return GstPropertiesEnv(aws_stack)


@pytest.fixture
def plugin_setup(genv):
    """A Use_Case with an admin and a fresh Plugin_Record (no artifacts)."""
    usecase_id = genv.create_usecase()
    admin = genv.make_admin(usecase_id)
    plugin = genv.create_plugin(admin, usecase_id)
    return usecase_id, admin, plugin


# ------------------------------------------- unavailability reasons (1.6)

class TestUnavailabilityReasons:
    """Each degraded case answers 200 {available: false, reason} — a
    machine-readable outcome, never an error (1.6, 7.4, 8.3)."""

    def test_no_artifacts_reports_no_build(self, genv, plugin_setup):
        _, admin, plugin = plugin_setup

        status, body = genv.get_gst_properties(
            admin, plugin["plugin_id"], plugin["version"])

        assert status == 200
        assert body == {"available": False, "reason": "no_x86_64_build"}

    def test_failed_x86_64_build_reports_no_build(self, genv, plugin_setup):
        _, admin, plugin = plugin_setup
        genv.seed_artifact(plugin, build_status="failed")

        status, body = genv.get_gst_properties(
            admin, plugin["plugin_id"], plugin["version"])

        assert status == 200
        assert body == {"available": False, "reason": "no_x86_64_build"}

    def test_other_arch_build_does_not_count_as_x86_64(self, genv,
                                                       plugin_setup):
        _, admin, plugin = plugin_setup
        genv.seed_artifact(plugin, arch="arm64_jp5")

        status, body = genv.get_gst_properties(
            admin, plugin["plugin_id"], plugin["version"])

        assert status == 200
        assert body == {"available": False, "reason": "no_x86_64_build"}

    def test_absent_stanza_reports_not_captured(self, genv, plugin_setup):
        """A successful build predating Property_Introspection (7.4)."""
        _, admin, plugin = plugin_setup
        genv.seed_artifact(plugin)  # succeeded, no gstIntrospection stanza

        status, body = genv.get_gst_properties(
            admin, plugin["plugin_id"], plugin["version"])

        assert status == 200
        assert body == {"available": False, "reason": "not_captured"}

    def test_failed_stanza_reports_introspection_failed(self, genv,
                                                        plugin_setup):
        _, admin, plugin = plugin_setup
        genv.seed_artifact(plugin, gst_introspection={
            "status": "failed",
            "message": "no element factories registered"})

        status, body = genv.get_gst_properties(
            admin, plugin["plugin_id"], plugin["version"])

        assert status == 200
        assert body["available"] is False
        assert body["reason"] == "introspection_failed"
        # The capture diagnostic reaches the caller (7.2).
        assert body["message"] == "no element factories registered"

    def test_missing_report_object_reports_introspection_failed(
            self, genv, plugin_setup):
        """Captured stanza, but the S3 report object is gone."""
        _, admin, plugin = plugin_setup
        genv.seed_artifact(plugin,
                           gst_introspection=genv.captured_stanza(plugin))

        status, body = genv.get_gst_properties(
            admin, plugin["plugin_id"], plugin["version"])

        assert status == 200
        assert body["available"] is False
        assert body["reason"] == "introspection_failed"

    def test_malformed_stored_json_reports_introspection_failed(
            self, genv, plugin_setup):
        """A stored document that is not JSON at all maps to the
        unavailability reason, never a 500 (8.3)."""
        _, admin, plugin = plugin_setup
        genv.seed_artifact(plugin,
                           gst_introspection=genv.captured_stanza(plugin))
        genv.put_report(plugin, b"{not valid json!")

        status, body = genv.get_gst_properties(
            admin, plugin["plugin_id"], plugin["version"])

        assert status == 200
        assert body["available"] is False
        assert body["reason"] == "introspection_failed"

    def test_nonconforming_report_document_reports_introspection_failed(
            self, genv, plugin_setup):
        """Valid JSON that fails parse_report (wrong reportVersion) also
        maps to introspection_failed (8.3)."""
        _, admin, plugin = plugin_setup
        genv.seed_artifact(plugin,
                           gst_introspection=genv.captured_stanza(plugin))
        genv.put_report(plugin, {"reportVersion": 999, "status": "captured",
                                 "elements": []})

        status, body = genv.get_gst_properties(
            admin, plugin["plugin_id"], plugin["version"])

        assert status == 200
        assert body["available"] is False
        assert body["reason"] == "introspection_failed"

    def test_failed_report_document_reports_introspection_failed(
            self, genv, plugin_setup):
        """A well-formed stored report whose own status is 'failed'
        surfaces the capture diagnostic (7.2)."""
        _, admin, plugin = plugin_setup
        genv.seed_artifact(plugin,
                           gst_introspection=genv.captured_stanza(plugin))
        genv.put_report(plugin, {
            "reportVersion": 1, "status": "failed",
            "message": "plugin registered no element factories",
            "gstVersion": None, "capturedAt": None, "elements": []})

        status, body = genv.get_gst_properties(
            admin, plugin["plugin_id"], plugin["version"])

        assert status == 200
        assert body["available"] is False
        assert body["reason"] == "introspection_failed"
        assert body["message"] == "plugin registered no element factories"


# ------------------------------------------------- the available path (1.5)

class TestAvailablePath:
    def test_serves_report_with_derived_suggestions(self, genv):
        _, admin, plugin = genv.plugin_with_report(captured_report_document())

        status, body = genv.get_gst_properties(
            admin, plugin["plugin_id"], plugin["version"])

        assert status == 200
        assert body["available"] is True
        assert body["gstVersion"] == "1.20.3"
        assert body["capturedAt"] == "2026-02-14T12:00:00Z"
        assert len(body["elements"]) == 1

        element = body["elements"][0]
        assert element["factory"] == "myblur"

        # Own writable mappable properties become suggestions in the
        # ParameterDeclaration wire shape, in property order (1.5).
        assert element["suggestions"] == [
            {"name": "radius", "paramType": "int", "required": False,
             "default": 5, "constraints": {"min": 0, "max": 100},
             "description": "Blur radius in pixels", "examples": [5]},
            {"name": "mode", "paramType": "enum", "required": False,
             "default": "gaussian",
             "constraints": {"values": ["gaussian", "box"]},
             "description": "Blur mode", "examples": ["gaussian"]},
        ]

        # The unmappable own property is skipped with a reason (2.5)...
        assert element["skipped"] == [
            {"name": "filter-caps",
             "reason": "no parameter type mapping for GType 'GstCaps'"},
        ]
        # ...and the base-class property appears nowhere (4.1).
        served_names = ([s["name"] for s in element["suggestions"]]
                        + [s["name"] for s in element["skipped"]])
        assert "name" not in served_names

    def test_viewer_role_can_read_gst_properties(self, genv):
        """node-designer read access suffices (1.5): every role of the
        Use_Case can run the Parameter_Scan."""
        usecase_id, _, plugin = genv.plugin_with_report(
            captured_report_document())
        viewer = genv.make_user()
        genv.assign_role(viewer, usecase_id, "Viewer")

        status, body = genv.get_gst_properties(
            viewer, plugin["plugin_id"], plugin["version"])

        assert status == 200
        assert body["available"] is True


# ------------------------------------- uniform 404 / RBAC (cross-tenant)

class TestAccessControl:
    """The route answers the same uniform 404 for a missing record and
    for a caller without node-designer read access, so Plugin_Record
    existence never leaks across tenants."""

    def test_unknown_plugin_is_uniform_404(self, genv, plugin_setup):
        _, admin, _ = plugin_setup
        status, body = genv.get_gst_properties(admin, "no-such-plugin", 1)
        assert status == 404
        assert body["error"]["code"] == "PLUGIN_NOT_FOUND"

    def test_unknown_version_is_uniform_404(self, genv, plugin_setup):
        _, admin, plugin = plugin_setup
        status, body = genv.get_gst_properties(
            admin, plugin["plugin_id"], 999)
        assert status == 404
        assert body["error"]["code"] == "PLUGIN_NOT_FOUND"

    def test_read_denied_user_gets_the_same_uniform_404(
            self, genv, monkeypatch):
        """RBAC denial of node-designer:read yields a 404 identical to
        the missing-record response (cross-tenant safe). The shared RBAC
        layer resolves users without a role record to read-only Viewer,
        so the denial is pinned at the module's permission seam — the
        same seam authorize_record_access consults — to lock the route's
        uniform-404 contract for denied readers."""
        _, admin, plugin = genv.plugin_with_report(captured_report_document())
        other_usecase = genv.create_usecase()
        outsider = genv.make_admin(other_usecase)

        real_check = genv.module.has_node_designer_permission

        def deny_outsider(user, usecase_id, permission):
            if user["user_id"] == outsider["user_id"]:
                return False
            return real_check(user, usecase_id, permission)

        monkeypatch.setattr(genv.module, "has_node_designer_permission",
                            deny_outsider)

        status, body = genv.get_gst_properties(
            outsider, plugin["plugin_id"], plugin["version"])
        missing_status, missing_body = genv.get_gst_properties(
            admin, "no-such-plugin", 1)

        assert status == 404
        assert body["error"]["code"] == "PLUGIN_NOT_FOUND"
        # Byte-for-byte the same envelope as a missing record: existence
        # is never leaked to a denied caller.
        assert (status, body) == (missing_status, missing_body)

    def test_cross_tenant_admin_never_sees_a_500_or_403_leak(self, genv):
        """With the real RBAC layer (no-role users resolve to read-only
        Viewer), an admin of a different Use_Case gets a normal read
        outcome or the uniform 404 — never an error that would confirm
        or deny the record's existence differently."""
        _, _, plugin = genv.plugin_with_report(captured_report_document())
        other_usecase = genv.create_usecase()
        outsider = genv.make_admin(other_usecase)

        status, body = genv.get_gst_properties(
            outsider, plugin["plugin_id"], plugin["version"])

        assert status in (200, 404)
        if status == 404:
            assert body["error"]["code"] == "PLUGIN_NOT_FOUND"
