"""
Unit tests for custom_node_types.py (custom-node-designer task 9.1).

Covers Custom_Node_Type registration with declaration validation and
plugin-dependency recording (8.1, 8.2, 8.5, 8.6), versioned declaration
updates retaining prior versions (14.1), deprecation (14.3), and
reference-checked removal deleting catalog items, Plugin_Library
artifacts, and Plugin_Component versions or rejecting with the
referencing workflows listed (14.4, 14.5).

Runs against the moto-backed stack from conftest.py (CustomNodeTypes,
PluginRecords, WorkflowVersions tables + portal artifacts bucket) with a
MagicMock standing in for the Use_Case account Greengrass registry.
"""
import hashlib
import json
import uuid
from unittest.mock import MagicMock

import pytest

from conftest import TEST_ENV


def make_declaration(type_id, archs=("x86_64",), **overrides):
    """A valid Custom_Node_Type wire declaration (design data model)."""
    declaration = {
        "typeId": type_id,
        "category": "preprocessing",
        "displayName": "Blur Regions",
        "inputs": [{"name": "in", "portType": "VideoFrames"}],
        "outputs": [{"name": "out", "portType": "VideoFrames"}],
        "parameters": [{
            "name": "radius",
            "paramType": "int",
            "required": True,
            "default": 5,
            "constraints": {"min": 1, "max": 64},
            "description": "Blur kernel radius in pixels",
            "examples": [5, 9],
        }],
        "mappings": [{
            "arch": arch,
            "elementChain": [{"factory": "blurregions",
                              "argsTemplate": {"radius": "{radius}"}}],
        } for arch in archs],
        "hardwareDependent": False,
    }
    declaration.update(overrides)
    return declaration


class NodeTypesEnv:
    """Facade for invoking the Custom_Node_Type API in tests."""

    def __init__(self, stack, monkeypatch):
        self.stack = stack
        self.module = stack.custom_node_types
        self.records = stack.plugin_records
        self.s3 = stack.s3
        self.bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]
        self.monkeypatch = monkeypatch

        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.usecase_bucket = f"usecase-bucket-{uuid.uuid4()}"
        self.s3.create_bucket(Bucket=self.usecase_bucket)
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Node Types Test Use Case",
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

    def seed_plugin(self, built_archs=("x86_64",), name="blur-regions"):
        """Create a Plugin_Record with successful builds promoted to the
        portal Plugin_Library."""
        response = self.records.handler({
            "httpMethod": "POST",
            "resource": "/plugins",
            "path": "/plugins",
            "pathParameters": None,
            "queryStringParameters": None,
            "body": json.dumps({"usecase_id": self.usecase_id,
                                "name": name, "kind": "scaffold"}),
            "requestContext": self._claims(self.admin),
        }, None)
        assert response["statusCode"] == 201, response["body"]
        plugin = json.loads(response["body"])["plugin"]

        artifacts = {}
        for arch in built_archs:
            data = f"\x7fELF {name} {arch}".encode()
            key = (f"workflow-plugins/custom/{self.usecase_id}/{arch}/"
                   f"{name}.so")
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)
            self.s3.put_object(Bucket=self.bucket, Key=key + ".sig",
                               Body=b"signature")
            artifacts[arch] = {
                "buildStatus": "succeeded", "s3Key": key,
                "checksum": hashlib.sha256(data).hexdigest(),
                "signature": "c2ln", "logTail": "",
            }
        self.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin["plugin_id"], "version": plugin["version"]},
            UpdateExpression="SET artifacts = :a",
            ExpressionAttributeValues={":a": artifacts},
        )
        return plugin

    def seed_workflow_version(self, definition, workflow_id=None, version=1):
        """A saved WorkflowVersions item with its S3 definition document."""
        workflow_id = workflow_id or f"wf-{uuid.uuid4()}"
        s3_key = (f"workflows/{self.usecase_id}/{workflow_id}/versions/"
                  f"{version}/workflow.json")
        self.s3.put_object(Bucket=self.bucket, Key=s3_key,
                           Body=json.dumps(definition).encode("utf-8"))
        self.stack.tables.versions.put_item(Item={
            "workflow_id": workflow_id,
            "version": version,
            "s3_definition_key": s3_key,
        })
        return workflow_id

    def patch_usecase_clients(self, greengrass=None):
        """MagicMock Greengrass + moto S3 for the Use_Case account."""
        gg = greengrass or MagicMock(name="greengrassv2")
        if greengrass is None:
            gg.list_component_versions.return_value = {"componentVersions": []}
        moto_s3 = self.s3

        def fake_get_usecase_client(service_name, usecase, session_name=None,
                                    region=None):
            return {"s3": moto_s3, "greengrassv2": gg}[service_name]

        self.monkeypatch.setattr(self.module, "get_usecase_client",
                                 fake_get_usecase_client)
        return gg

    def audit_entries(self, action):
        response = self.stack.tables.audit_log.scan()
        return [i for i in response["Items"] if i["action"] == action]

    # ----------------------------------------------------------- invoke
    def _claims(self, user):
        return {"authorizer": {"claims": {
            "sub": user["user_id"],
            "email": user["email"],
            "cognito:username": user["username"],
            "custom:role": user["role"],
        }}}

    def invoke(self, method, resource, user, node_type_id=None, body=None,
               query=None):
        event = {
            "httpMethod": method,
            "resource": resource,
            "path": resource.replace("{id}", node_type_id or ""),
            "pathParameters": {"id": node_type_id} if node_type_id else None,
            "queryStringParameters": query,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": self._claims(user),
        }
        response = self.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    # ------------------------------------------------------ conveniences
    def register(self, user, plugin, declaration, **extra):
        body = {"plugin_id": plugin["plugin_id"],
                "plugin_version": plugin["version"],
                "declaration": declaration}
        body.update(extra)
        return self.invoke("POST", "/custom-node-types", user, body=body)

    def get(self, user, node_type_id):
        return self.invoke("GET", "/custom-node-types/{id}", user, node_type_id)

    def list_by_plugin(self, user, plugin_id=None):
        query = {"plugin_id": plugin_id} if plugin_id else None
        return self.invoke("GET", "/custom-node-types", user, query=query)

    def update(self, user, node_type_id, **body):
        return self.invoke("PUT", "/custom-node-types/{id}", user,
                           node_type_id, body=body)

    def deprecate(self, user, node_type_id, **body):
        return self.invoke("POST", "/custom-node-types/{id}/deprecate", user,
                           node_type_id, body=body)

    def remove(self, user, node_type_id):
        return self.invoke("DELETE", "/custom-node-types/{id}", user,
                           node_type_id)

    def table_items(self, node_type_id):
        from boto3.dynamodb.conditions import Key
        response = self.stack.tables.custom_node_types.query(
            KeyConditionExpression=Key("node_type_id").eq(node_type_id))
        return response["Items"]


@pytest.fixture
def nenv(aws_stack, monkeypatch):
    return NodeTypesEnv(aws_stack, monkeypatch)


# ---------------------------------------------------------------- 8.1/8.6

class TestRegistration:
    def test_registration_collects_declaration_and_records_dependency(self, nenv):
        """Registration stores the full declaration and records the plugin
        dependency custom:{usecase_id}/{plugin_name} in every mapping
        (8.1, 8.6)."""
        plugin = nenv.seed_plugin(built_archs=("x86_64", "arm64_jp5"))
        declaration = make_declaration("custom.blur_regions",
                                       archs=("x86_64", "arm64_jp5"))

        status, body = nenv.register(nenv.admin, plugin, declaration)

        assert status == 201, body
        node_type = body["nodeType"]
        assert node_type["node_type_id"] == "custom.blur_regions"
        assert node_type["version"] == 1
        assert node_type["plugin_id"] == plugin["plugin_id"]
        assert node_type["plugin_version"] == plugin["version"]
        assert node_type["usecase_ids"] == [nenv.usecase_id]
        assert node_type["deprecated"] is False

        stored = node_type["declaration"]
        assert stored["displayName"] == "Blur Regions"
        assert stored["category"] == "preprocessing"
        assert stored["parameters"][0]["description"]
        assert stored["parameters"][0]["examples"]
        assert stored["hardwareDependent"] is False
        dependency = f"custom:{nenv.usecase_id}/blur-regions"
        for mapping in stored["mappings"]:
            assert dependency in mapping["pluginDependencies"]

        assert nenv.audit_entries("register_custom_node_type")

    def test_invalid_port_declaration_rejected_identifying_offense(self, nenv):
        """An out-of-catalog Port type is rejected with the offending
        field identified (8.5)."""
        plugin = nenv.seed_plugin()
        declaration = make_declaration(
            "custom.bad_port",
            inputs=[{"name": "in", "portType": "NotAPortType"}])

        status, body = nenv.register(nenv.admin, plugin, declaration)

        assert status == 400
        assert body["error"]["code"] == "INVALID_DECLARATION"
        assert body["error"]["details"]["field"] == "inputs[0].portType"
        assert nenv.table_items("custom.bad_port") == []

    def test_mapping_for_unbuilt_architecture_rejected(self, nenv):
        """Mappings are collected per *built* Target_Architecture (8.1):
        an arch without a successful Plugin_Artifact is rejected."""
        plugin = nenv.seed_plugin(built_archs=("x86_64",))
        declaration = make_declaration("custom.unbuilt",
                                       archs=("x86_64", "arm64_jp6"))

        status, body = nenv.register(nenv.admin, plugin, declaration)

        assert status == 400
        assert body["error"]["code"] == "UNBUILT_ARCHITECTURE"
        assert body["error"]["details"]["field"] == "mappings[1].arch"
        assert body["error"]["details"]["built_architectures"] == ["x86_64"]

    def test_duplicate_type_id_conflicts(self, nenv):
        plugin = nenv.seed_plugin()
        declaration = make_declaration("custom.dupe")
        status, _ = nenv.register(nenv.admin, plugin, declaration)
        assert status == 201

        status, body = nenv.register(nenv.admin, plugin, declaration)
        assert status == 409
        assert body["error"]["code"] == "TYPE_ID_CONFLICT"

    def test_builtin_type_id_collision_rejected(self, nenv):
        """A custom type may never collide with a built-in catalog type."""
        from workflow_core.catalog.nodes import NODE_CATALOG
        plugin = nenv.seed_plugin()
        declaration = make_declaration(NODE_CATALOG[0].type_id)

        status, body = nenv.register(nenv.admin, plugin, declaration)
        assert status == 409
        assert body["error"]["code"] == "TYPE_ID_CONFLICT"

    def test_registration_requires_register_permission(self, nenv):
        """DataScientist may read but not register (13.1/13.4)."""
        plugin = nenv.seed_plugin()
        scientist = nenv.make_user()
        nenv.assign_role(scientist, "DataScientist")

        status, body = nenv.register(scientist, plugin,
                                     make_declaration("custom.denied"))
        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"


# ------------------------------------------------------------------- 14.1

class TestVersioning:
    def test_update_creates_new_version_retaining_prior(self, nenv):
        """A declaration update creates a new version item; prior versions
        stay retrievable (14.1)."""
        plugin = nenv.seed_plugin()
        type_id = "custom.versioned"
        status, body = nenv.register(nenv.admin, plugin,
                                     make_declaration(type_id))
        assert status == 201

        updated_declaration = make_declaration(type_id,
                                               displayName="Blur Regions v2")
        status, body = nenv.update(nenv.admin, type_id,
                                   declaration=updated_declaration)
        assert status == 201, body
        assert body["nodeType"]["version"] == 2
        assert body["nodeType"]["declaration"]["displayName"] == "Blur Regions v2"

        status, body = nenv.get(nenv.admin, type_id)
        assert status == 200
        versions = body["versions"]
        assert [v["version"] for v in versions] == [2, 1]
        assert body["nodeType"]["version"] == 2
        assert nenv.audit_entries("update_custom_node_type")

    def test_update_rejects_type_id_change(self, nenv):
        plugin = nenv.seed_plugin()
        status, _ = nenv.register(nenv.admin, plugin,
                                  make_declaration("custom.fixed_id"))
        assert status == 201

        status, body = nenv.update(
            nenv.admin, "custom.fixed_id",
            declaration=make_declaration("custom.other_id"))
        assert status == 400
        assert body["error"]["code"] == "TYPE_ID_MISMATCH"


# ------------------------------------------------------------------- 14.3

class TestDeprecation:
    def test_deprecate_flips_flag_on_every_version(self, nenv):
        plugin = nenv.seed_plugin()
        type_id = "custom.deprecated"
        status, _ = nenv.register(nenv.admin, plugin, make_declaration(type_id))
        assert status == 201
        status, _ = nenv.update(nenv.admin, type_id,
                                declaration=make_declaration(type_id))
        assert status == 201

        status, body = nenv.deprecate(nenv.admin, type_id)
        assert status == 200, body
        assert body["nodeType"]["deprecated"] is True
        assert all(v["deprecated"] for v in body["versions"])
        assert nenv.audit_entries("deprecate_custom_node_type")

    def test_deprecation_requires_manage_permission(self, nenv):
        plugin = nenv.seed_plugin()
        type_id = "custom.deprecate_denied"
        status, _ = nenv.register(nenv.admin, plugin, make_declaration(type_id))
        assert status == 201

        operator = nenv.make_user()
        nenv.assign_role(operator, "Operator")
        status, body = nenv.deprecate(operator, type_id)
        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"


# -------------------------------------------------------------- 14.4/14.5

class TestRemoval:
    def test_removal_with_zero_references_deletes_everything(self, nenv):
        """Zero WorkflowVersions references: catalog items, Plugin_Library
        artifacts, and Plugin_Component versions are deleted (14.4)."""
        plugin = nenv.seed_plugin(built_archs=("x86_64", "arm64_jp5"),
                                  name="removable")
        type_id = "custom.removable"
        status, _ = nenv.register(nenv.admin, plugin,
                                  make_declaration(type_id,
                                                   archs=("x86_64", "arm64_jp5")))
        assert status == 201

        # An unrelated saved workflow must not block removal.
        nenv.seed_workflow_version(
            {"nodes": [{"id": "n1", "type": "video_input"}]})

        version_arn = ("arn:aws:greengrass:us-east-1:123456789012:components:"
                       f"dda.plugin.{plugin['plugin_id']}:versions:1.0.0")
        gg = MagicMock(name="greengrassv2")
        gg.list_component_versions.return_value = {
            "componentVersions": [{"arn": version_arn}]}
        nenv.patch_usecase_clients(gg)

        status, body = nenv.remove(nenv.admin, type_id)
        assert status == 200, body
        assert body["removed"] is True
        assert body["versions_removed"] == [1]

        # Catalog items gone.
        assert nenv.table_items(type_id) == []
        # Plugin_Library artifacts (.so + .sig) gone.
        listed = nenv.s3.list_objects_v2(
            Bucket=nenv.bucket,
            Prefix=f"workflow-plugins/custom/{nenv.usecase_id}/")
        remaining = [o["Key"] for o in listed.get("Contents", [])
                     if "/removable.so" in o["Key"]]
        assert remaining == []
        # Plugin_Component versions deleted in the Use_Case registry.
        gg.delete_component.assert_called_once_with(arn=version_arn)
        assert nenv.audit_entries("remove_custom_node_type")

    def test_removal_with_references_rejected_listing_workflows(self, nenv):
        """A referencing saved workflow blocks removal; the rejection lists
        exactly the referencing workflows (14.5)."""
        plugin = nenv.seed_plugin(name="in-use")
        type_id = "custom.in_use"
        status, _ = nenv.register(nenv.admin, plugin, make_declaration(type_id))
        assert status == 201

        workflow_id = nenv.seed_workflow_version(
            {"nodes": [{"id": "n1", "type": type_id}]})
        nenv.patch_usecase_clients()

        status, body = nenv.remove(nenv.admin, type_id)
        assert status == 409, body
        assert body["error"]["code"] == "CUSTOM_NODE_TYPE_IN_USE"
        referencing = body["error"]["details"]["referencing_workflows"]
        assert {"workflow_id": workflow_id, "version": 1} in referencing

        # Nothing deleted: catalog items and artifacts intact.
        assert len(nenv.table_items(type_id)) == 1
        listed = nenv.s3.list_objects_v2(
            Bucket=nenv.bucket,
            Prefix=f"workflow-plugins/custom/{nenv.usecase_id}/x86_64/in-use.so")
        assert listed.get("KeyCount", 0) >= 1

    def test_reference_scan_honors_saved_reference_attribute(self, nenv):
        """Items carrying the custom_node_types attribute recorded at save
        (task 9.2) are honored without loading the definition."""
        plugin = nenv.seed_plugin(name="attr-ref")
        type_id = "custom.attr_ref"
        status, _ = nenv.register(nenv.admin, plugin, make_declaration(type_id))
        assert status == 201

        workflow_id = f"wf-{uuid.uuid4()}"
        nenv.stack.tables.versions.put_item(Item={
            "workflow_id": workflow_id,
            "version": 3,
            "custom_node_types": {type_id: 1},
        })

        status, body = nenv.remove(nenv.admin, type_id)
        assert status == 409
        referencing = body["error"]["details"]["referencing_workflows"]
        assert {"workflow_id": workflow_id, "version": 3} in referencing


# ------------------------------------------------------------ pure helpers

class TestPureHelpers:
    def test_inject_plugin_dependency_is_idempotent(self, nenv):
        module = nenv.module
        declaration = make_declaration("custom.pure")
        dependency = "custom:uc-1/pure"

        once = module.inject_plugin_dependency(declaration, dependency)
        twice = module.inject_plugin_dependency(once, dependency)
        assert once == twice
        assert all(m["pluginDependencies"].count(dependency) == 1
                   for m in twice["mappings"])
        # The input declaration is never mutated.
        assert "pluginDependencies" not in declaration["mappings"][0]

    def test_evaluate_removal_decision(self, nenv):
        module = nenv.module
        assert module.evaluate_removal("custom.x", []) is None

        rejection = module.evaluate_removal(
            "custom.x", [{"workflow_id": "wf-1", "version": 2}])
        assert rejection["code"] == "CUSTOM_NODE_TYPE_IN_USE"
        assert rejection["details"]["referencing_workflows"] == [
            {"workflow_id": "wf-1", "version": 2}]

    def test_definition_reference_detection(self, nenv):
        module = nenv.module
        assert module.definition_references_node_type(
            {"nodes": [{"id": "a", "type": "custom.x"}]}, "custom.x")
        assert not module.definition_references_node_type(
            {"nodes": [{"id": "a", "type": "video_input"}]}, "custom.x")
        assert not module.definition_references_node_type(None, "custom.x")


# --------------------------------------------------- list by backing plugin

class TestListByPlugin:
    """GET /custom-node-types?plugin_id=... — the registration wizard's
    duplicate detection: a plugin already backing a node type is updated
    instead of re-registered."""

    def test_lists_latest_version_of_types_backed_by_plugin(self, nenv):
        # The moto CustomNodeTypes table persists across tests in the
        # session-scoped stack, so the type id is unique to this test.
        type_id = f"custom.listed_{uuid.uuid4().hex[:8]}"
        plugin = nenv.seed_plugin(name="listed-plugin")
        declaration = make_declaration(type_id)
        status, _ = nenv.register(nenv.admin, plugin, declaration)
        assert status == 201

        status, body = nenv.list_by_plugin(nenv.admin, plugin["plugin_id"])
        assert status == 200
        assert body["count"] == 1
        summary = body["nodeTypes"][0]
        assert summary["node_type_id"] == type_id
        assert summary["version"] == 1
        assert summary["plugin_id"] == plugin["plugin_id"]

        # An update creates version 2; the list still returns one entry,
        # now at the latest version (14.1).
        status, _ = nenv.update(nenv.admin, type_id, declaration=declaration)
        assert status == 201
        status, body = nenv.list_by_plugin(nenv.admin, plugin["plugin_id"])
        assert status == 200
        assert body["count"] == 1
        assert body["nodeTypes"][0]["version"] == 2

    def test_plugin_without_registrations_lists_empty(self, nenv):
        plugin = nenv.seed_plugin(name="fresh-plugin")
        status, body = nenv.list_by_plugin(nenv.admin, plugin["plugin_id"])
        assert status == 200
        assert body == {"nodeTypes": [], "count": 0}

    def test_missing_plugin_id_is_rejected(self, nenv):
        status, body = nenv.list_by_plugin(nenv.admin)
        assert status == 400
        assert body["error"]["code"] == "MISSING_PLUGIN_ID"

    def test_unknown_plugin_is_not_found(self, nenv):
        status, body = nenv.list_by_plugin(nenv.admin, "no-such-plugin")
        assert status == 404
        assert body["error"]["code"] == "PLUGIN_NOT_FOUND"
