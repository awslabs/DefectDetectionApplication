"""
RBAC and audit tests (Workflow Manager).

Task 7.5 (spec: workflow-manager).

1. Parameterized role x action matrix covering every portal role
   (Viewer, Operator, DataScientist, UseCaseAdmin, PortalAdmin) against
   every workflow action (read/create/edit/save/delete/test/package/
   deploy), asserting rbac_manager.has_permission agrees with the design
   matrix - both for JWT-supplied roles and for Use_Case role
   assignments in the UserRoles table (Requirements 11.1, 11.2, 11.3,
   11.4). Endpoint-level spot checks run through the real handlers
   (workflows.handler, workflow_packaging.handler, deployments.handler).

2. Audit log writes: create/modify/delete via workflows.handler,
   packaging success/failure via workflow_packaging.handler, and deploy
   via deployments.handler each write a record to the existing audit
   log table with the action, the acting user, and a timestamp
   (Requirement 11.5). Denied operations write an unauthorized_access
   record (Requirement 11.4).

Runs against the shared moto stack from conftest.py; handler modules
are imported inside the mock so their module-level boto3 clients are
intercepted. For permission-denial endpoint tests the mocked Use_Case
account clients are never reached.

_Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_
"""
import json
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from conftest import REGION, TEST_ENV


# ---------------------------------------------------------------------------
# Design RBAC matrix (design.md section 11, Requirements 11.1-11.3)
# ---------------------------------------------------------------------------

ACTIONS = ("read", "create", "edit", "save", "delete", "test", "package", "deploy")

ACTION_PERMISSION = {
    "read": "WORKFLOW_READ",
    "create": "WORKFLOW_CREATE",
    "edit": "WORKFLOW_EDIT",
    "save": "WORKFLOW_SAVE",
    "delete": "WORKFLOW_DELETE",
    "test": "WORKFLOW_TEST",
    "package": "WORKFLOW_PACKAGE",
    "deploy": "WORKFLOW_DEPLOY",
}

ROLE_ALLOWED_ACTIONS = {
    # Viewer: read-only view of workflows and deployment status (11.3)
    "Viewer": {"read"},
    # Operator: package and deploy (11.2)
    "Operator": {"read", "package", "deploy"},
    # DataScientist: create, edit, save (11.1) plus delete and test
    "DataScientist": {"read", "create", "edit", "save", "delete", "test"},
    # UseCaseAdmin: all workflow actions (11.1, 11.2)
    "UseCaseAdmin": set(ACTIONS),
    # PortalAdmin: everything
    "PortalAdmin": set(ACTIONS),
}

ROLES = tuple(ROLE_ALLOWED_ACTIONS)

MATRIX_CASES = [(role, action) for role in ROLES for action in ACTIONS]
MATRIX_IDS = [f"{role}-{action}" for role, action in MATRIX_CASES]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mods(aws_stack):
    """Import the modules under test while the moto mock is active.

    Follows the conftest pattern: pop previously imported copies so the
    fresh imports bind moto-intercepted module-level boto3 clients.
    (aws_stack already imported the real shared_utils + workflows.)
    """
    for module_name in ("workflow_guards", "workflow_packaging", "deployments"):
        sys.modules.pop(module_name, None)
    import shared_utils
    import workflow_packaging
    import deployments
    return SimpleNamespace(
        shared_utils=shared_utils,
        rbac_manager=shared_utils.rbac_manager,
        Permission=shared_utils.Permission,
        workflows=aws_stack.workflows,
        packaging=workflow_packaging,
        deployments=deployments,
    )


@pytest.fixture
def audit_table(aws_stack):
    """The moto-backed audit log table shared_utils.log_audit_event writes to."""
    import boto3

    return boto3.resource("dynamodb", region_name=REGION).Table(
        TEST_ENV["AUDIT_LOG_TABLE"]
    )


def audit_events(audit_table, user_id, action=None):
    """All audit records written for one acting user (each test uses a
    fresh uuid-based user, so this isolates per-test events)."""
    items, kwargs = [], {}
    while True:
        response = audit_table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    events = [i for i in items if i.get("user_id") == user_id]
    if action is not None:
        events = [e for e in events if e.get("action") == action]
    return events


# ---------------------------------------------------------------------------
# Workflow definitions
# ---------------------------------------------------------------------------

def make_definition(topic="line-1/results"):
    """Minimal valid Workflow_Definition (camera -> mqtt)."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "cam", "type": "camera_source",
             "position": {"x": 0, "y": 0}, "parameters": {}},
            {"id": "out", "type": "mqtt_publish",
             "position": {"x": 400, "y": 120}, "parameters": {"topic": topic}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "cam", "port": "video"},
             "to": {"node": "out", "port": "in"}},
        ],
    }


def make_plugin_definition():
    """folder_source -> dewarp -> capture; dewarp requires the non-bundled
    'dda-dewarp' plugin, exercising the plugin-library packaging path."""
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


def create_workflow(env, user, usecase_id, definition=None, name="RBAC test workflow"):
    status, payload = env.invoke("POST", "/workflows", user, body={
        "usecase_id": usecase_id,
        "name": name,
        "description": "rbac/audit test",
        "definition": definition or make_definition(),
    })
    assert status == 201, payload
    return payload["workflow"]["workflow_id"]


def mark_version_validated(env, workflow_id, version=1, **extra):
    """Record a passed Workflow_Validator run (and optional extra
    attributes) on a WorkflowVersions item."""
    update = "SET validation_status = :v"
    values = {":v": {"status": "passed", "validated_at": 1}}
    for i, (attr, value) in enumerate(extra.items()):
        update += f", {attr} = :x{i}"
        values[f":x{i}"] = value
    env.stack.tables.versions.update_item(
        Key={"workflow_id": workflow_id, "version": version},
        UpdateExpression=update,
        ExpressionAttributeValues=values,
    )


# ===========================================================================
# 1. Role x action permission matrix (Requirements 11.1, 11.2, 11.3, 11.4)
# ===========================================================================

class TestRoleActionMatrix:
    """rbac_manager.has_permission agrees with the design matrix for every
    role x workflow-action combination."""

    @pytest.mark.parametrize("role,action", MATRIX_CASES, ids=MATRIX_IDS)
    def test_jwt_role_matches_design_matrix(self, mods, role, action):
        """Role supplied via JWT claims (custom:role), no Use_Case
        assignment rows (Requirements 11.1-11.4)."""
        user_id = f"user-{uuid.uuid4()}"
        usecase_id = f"uc-{uuid.uuid4()}"
        permission = mods.Permission[ACTION_PERMISSION[action]]
        expected = action in ROLE_ALLOWED_ACTIONS[role]

        granted = mods.rbac_manager.has_permission(
            user_id, usecase_id, permission, user_info={"role": role})

        assert granted is expected, (
            f"{role} {'should' if expected else 'must not'} hold "
            f"workflow:{action}")

    @pytest.mark.parametrize("role,action", MATRIX_CASES, ids=MATRIX_IDS)
    def test_usecase_assigned_role_matches_design_matrix(self, env, mods, role, action):
        """Role assigned per Use_Case in the UserRoles table takes
        precedence over the JWT default (here Viewer) - the matrix must
        hold for Use_Case role assignments too (Requirements 11.1-11.3)."""
        user = env.make_user(role="Viewer")
        usecase_id = f"uc-{uuid.uuid4()}"
        env.assign_role(user, usecase_id, role)
        permission = mods.Permission[ACTION_PERMISSION[action]]
        expected = action in ROLE_ALLOWED_ACTIONS[role]

        granted = mods.rbac_manager.has_permission(
            user["user_id"], usecase_id, permission, user_info=user)

        assert granted is expected

    def test_usecase_role_does_not_leak_to_other_usecases(self, env, mods):
        """A Use_Case role assignment grants permissions only within that
        Use_Case (Requirements 11.1, 11.4)."""
        user = env.make_user(role="Viewer")
        assigned_usecase = f"uc-{uuid.uuid4()}"
        other_usecase = f"uc-{uuid.uuid4()}"
        env.assign_role(user, assigned_usecase, "UseCaseAdmin")

        permission = mods.Permission.WORKFLOW_CREATE
        assert mods.rbac_manager.has_permission(
            user["user_id"], assigned_usecase, permission, user_info=user) is True
        assert mods.rbac_manager.has_permission(
            user["user_id"], other_usecase, permission, user_info=user) is False

    @pytest.mark.parametrize("role", ROLES)
    def test_bedrock_config_write_is_portal_admin_only(self, mods, role):
        """bedrock-config:write belongs to PortalAdmin only (design
        section 11 matrix)."""
        user_id = f"user-{uuid.uuid4()}"
        usecase_id = f"uc-{uuid.uuid4()}"
        granted = mods.rbac_manager.has_permission(
            user_id, usecase_id, mods.Permission.BEDROCK_CONFIG_WRITE,
            user_info={"role": role})
        assert granted is (role == "PortalAdmin")


# ===========================================================================
# 1b. Endpoint-level RBAC spot checks through the real handlers (11.4)
# ===========================================================================

class TestWorkflowsHandlerRbac:
    """workflows.handler create/delete spot checks."""

    def test_operator_cannot_create_workflow(self, env):
        """Operator lacks workflow:create -> 403 (Requirements 11.1, 11.4)."""
        operator = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        status, payload = env.invoke("POST", "/workflows", operator, body={
            "usecase_id": usecase_id,
            "name": "nope",
            "definition": make_definition(),
        })
        assert status == 403
        assert payload["error"]["code"] == "FORBIDDEN"
        assert ("workflow:create"
                in payload["error"]["details"]["required_permissions"])

    def test_operator_cannot_delete_but_can_read(self, env):
        """Operator lacks workflow:delete but holds workflow:read
        (Requirements 11.2, 11.3, 11.4)."""
        creator = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        workflow_id = create_workflow(env, creator, usecase_id)

        operator = env.make_user(role="Operator")
        status, payload = env.invoke(
            "DELETE", "/workflows/{id}", operator, workflow_id=workflow_id)
        assert status == 403
        assert payload["error"]["code"] == "FORBIDDEN"

        status, _ = env.invoke(
            "GET", "/workflows/{id}", operator, workflow_id=workflow_id)
        assert status == 200

    def test_data_scientist_can_create_edit_and_delete(self, env):
        """DataScientist creates, saves changes to, and deletes a workflow
        (Requirement 11.1)."""
        user = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        workflow_id = create_workflow(env, user, usecase_id)

        status, payload = env.invoke(
            "PUT", "/workflows/{id}", user, workflow_id=workflow_id,
            body={"definition": make_definition(topic="edited")})
        assert status == 200, payload

        status, payload = env.invoke(
            "DELETE", "/workflows/{id}", user, workflow_id=workflow_id)
        assert status == 200, payload


class TestPackagingHandlerRbac:
    """workflow_packaging.handler permission checks. For denials the
    Use_Case account clients are never reached, so nothing is mocked."""

    @pytest.fixture
    def packaged_workflow(self, env):
        creator = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        workflow_id = create_workflow(env, creator, usecase_id)
        return usecase_id, workflow_id

    def package(self, env, mods, user, workflow_id):
        event = env.event("POST", "/workflows/{id}/package", user,
                          workflow_id=workflow_id,
                          body={"architectures": ["x86_64"]})
        response = mods.packaging.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    @pytest.mark.parametrize("role", ["Viewer", "DataScientist"])
    def test_roles_without_package_permission_get_403(
            self, env, mods, packaged_workflow, role):
        """Viewer and DataScientist lack workflow:package -> 403
        (Requirements 11.2, 11.4)."""
        _, workflow_id = packaged_workflow
        user = env.make_user(role=role)
        status, payload = self.package(env, mods, user, workflow_id)
        assert status == 403
        assert payload["error"]["code"] == "FORBIDDEN"
        assert ("workflow:package"
                in payload["error"]["details"]["required_permissions"])

    def test_operator_passes_rbac_and_reaches_validation_guard(
            self, env, mods, packaged_workflow):
        """Operator holds workflow:package: the request is not denied and
        proceeds to the validation guard (409, not 403) (Requirement 11.2)."""
        _, workflow_id = packaged_workflow
        operator = env.make_user(role="Operator")
        status, payload = self.package(env, mods, operator, workflow_id)
        assert status == 409
        assert payload["error"]["code"] == "VALIDATION_REQUIRED"


class TestDeploymentsHandlerRbac:
    """deployments.handler workflow-deployment permission checks. For
    denials the mocked Greengrass/IoT clients are never reached."""

    def deploy(self, env, mods, user, usecase_id, workflow_id):
        event = env.event("POST", "/deployments", user, body={
            "component_type": "workflow",
            "usecase_id": usecase_id,
            "workflow_id": workflow_id,
            "target_devices": ["device-1"],
        })
        response = mods.deployments.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    @pytest.mark.parametrize("role", ["Viewer", "DataScientist"])
    def test_roles_without_deploy_permission_get_403(self, env, mods, role):
        """Viewer and DataScientist lack workflow:deploy -> 403
        (Requirements 11.2, 11.4)."""
        usecase_id = env.create_usecase()
        user = env.make_user(role=role)
        status, payload = self.deploy(env, mods, user, usecase_id, "wf-any")
        assert status == 403
        assert payload["error"]["code"] == "FORBIDDEN"
        assert ("workflow:deploy"
                in payload["error"]["details"]["required_permissions"])

    def test_operator_passes_rbac_check(self, env, mods):
        """Operator holds workflow:deploy: the request is not denied and
        proceeds to the workflow lookup (404, not 403) (Requirement 11.2)."""
        usecase_id = env.create_usecase()
        operator = env.make_user(role="Operator")
        status, payload = self.deploy(
            env, mods, operator, usecase_id, "wf-does-not-exist")
        assert status == 404
        assert payload["error"]["code"] == "WORKFLOW_NOT_FOUND"


# ===========================================================================
# 2. Audit log writes (Requirement 11.5, denials 11.4)
# ===========================================================================

def assert_audit_record(record, user_id, resource_id, result="success"):
    """Common Requirement 11.5 shape: action, acting user, timestamp."""
    assert record["user_id"] == user_id
    assert record["resource_type"] == "workflow"
    assert record["resource_id"] == resource_id
    assert record["result"] == result
    assert int(record["timestamp"]) > 0


class TestWorkflowStoreAudit:
    """Audit writes for create / modify / delete via workflows.handler."""

    def test_create_writes_audit_record(self, env, audit_table):
        """Creating a workflow records create_workflow with the acting
        user and a timestamp (Requirement 11.5)."""
        user = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        workflow_id = create_workflow(env, user, usecase_id, name="Audited")

        events = audit_events(audit_table, user["user_id"], "create_workflow")
        assert len(events) == 1
        assert_audit_record(events[0], user["user_id"], workflow_id)
        assert events[0]["details"]["usecase_id"] == usecase_id
        assert events[0]["details"]["name"] == "Audited"
        assert int(events[0]["details"]["version"]) == 1

    def test_modify_writes_audit_record(self, env, audit_table):
        """Saving changes records update_workflow with the new version
        (Requirement 11.5)."""
        user = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        workflow_id = create_workflow(env, user, usecase_id)

        status, _ = env.invoke(
            "PUT", "/workflows/{id}", user, workflow_id=workflow_id,
            body={"definition": make_definition(topic="revised")})
        assert status == 200

        events = audit_events(audit_table, user["user_id"], "update_workflow")
        assert len(events) == 1
        assert_audit_record(events[0], user["user_id"], workflow_id)
        assert int(events[0]["details"]["version"]) == 2

    def test_delete_writes_audit_record(self, env, audit_table):
        """Deleting a workflow records delete_workflow (Requirement 11.5)."""
        user = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        workflow_id = create_workflow(env, user, usecase_id)

        status, _ = env.invoke(
            "DELETE", "/workflows/{id}", user, workflow_id=workflow_id)
        assert status == 200

        events = audit_events(audit_table, user["user_id"], "delete_workflow")
        assert len(events) == 1
        assert_audit_record(events[0], user["user_id"], workflow_id)
        assert events[0]["details"]["usecase_id"] == usecase_id

    def test_denied_operation_writes_unauthorized_access_record(
            self, env, audit_table):
        """A denied workflow operation records unauthorized_access with
        result denied (Requirement 11.4)."""
        viewer = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        status, _ = env.invoke("POST", "/workflows", viewer, body={
            "usecase_id": usecase_id,
            "name": "denied",
            "definition": make_definition(),
        })
        assert status == 403

        events = audit_events(audit_table, viewer["user_id"], "unauthorized_access")
        assert len(events) == 1
        record = events[0]
        assert record["result"] == "denied"
        assert "workflow:create" in record["details"]["required_permissions"]
        assert record["details"]["usecase_id"] == usecase_id


class TestPackagingAudit:
    """Audit writes for packaging success and failure via
    workflow_packaging.handler (Requirement 11.5)."""

    @pytest.fixture
    def pkg(self, env, mods, monkeypatch):
        """A validated workflow whose definition needs the dda-dewarp
        plugin, a Use_Case with an S3 bucket, and a per-test plugin
        library prefix. Cross-account clients are patched with local
        moto S3 + a fake DEPLOYABLE Greengrass registry."""
        monkeypatch.setattr(mods.packaging, "COMPONENT_STATUS_POLL_SECONDS", 0)
        plugin_prefix = f"workflow-plugins-{uuid.uuid4()}"
        monkeypatch.setattr(
            mods.packaging, "WORKFLOW_PLUGIN_LIBRARY_PREFIX", plugin_prefix)

        user = env.make_user(role="UseCaseAdmin")
        usecase_bucket = f"usecase-bucket-{uuid.uuid4()}"
        env.s3.create_bucket(Bucket=usecase_bucket)
        usecase_id = f"uc-{uuid.uuid4()}"
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": usecase_id,
            "name": "Packaging Audit Test",
            "account_id": "123456789012",
            "s3_bucket": usecase_bucket,
        })
        workflow_id = create_workflow(
            env, user, usecase_id, definition=make_plugin_definition())
        mark_version_validated(env, workflow_id)

        greengrass = MagicMock(name="greengrassv2")
        greengrass.create_component_version.return_value = {
            "arn": ("arn:aws:greengrass:us-east-1:123456789012:"
                    f"components:test:versions:{uuid.uuid4()}")
        }
        greengrass.describe_component.return_value = {
            "status": {"componentState": "DEPLOYABLE", "message": ""}
        }

        def fake_get_usecase_client(service_name, usecase, session_name=None,
                                    region=None):
            if service_name == "s3":
                return env.s3
            if service_name == "greengrassv2":
                return greengrass
            raise AssertionError(f"unexpected usecase client: {service_name}")

        monkeypatch.setattr(
            mods.packaging, "get_usecase_client", fake_get_usecase_client)

        def seed_plugin_library():
            env.s3.put_object(
                Bucket=env.bucket,
                Key=f"{plugin_prefix}/x86_64/dda-dewarp.so",
                Body=b"\x7fELF fake plugin x86_64",
            )

        def package():
            event = env.event("POST", "/workflows/{id}/package", user,
                              workflow_id=workflow_id,
                              body={"architectures": ["x86_64"]})
            response = mods.packaging.handler(event, None)
            return response["statusCode"], json.loads(response["body"])

        return SimpleNamespace(
            user=user, usecase_id=usecase_id, workflow_id=workflow_id,
            seed_plugin_library=seed_plugin_library, package=package)

    def test_packaging_success_writes_audit_record(self, pkg, audit_table):
        """Successful packaging records package_workflow with result
        success, component identity, and a timestamp (Requirement 11.5)."""
        pkg.seed_plugin_library()
        status, payload = pkg.package()
        assert status == 201, payload

        events = audit_events(audit_table, pkg.user["user_id"], "package_workflow")
        assert len(events) == 1
        record = events[0]
        assert_audit_record(record, pkg.user["user_id"], pkg.workflow_id)
        assert record["details"]["usecase_id"] == pkg.usecase_id
        assert int(record["details"]["version"]) == 1
        assert record["details"]["component_name"] == f"dda.workflow.{pkg.workflow_id}"
        assert record["details"]["component_version"] == "1.0.0"

    def test_packaging_failure_writes_audit_record(self, pkg, audit_table):
        """A failed packaging attempt records package_workflow with result
        failure and the failing artifact (Requirement 11.5)."""
        # Plugin library deliberately not seeded -> missing artifact.
        status, payload = pkg.package()
        assert status == 502
        assert payload["error"]["code"] == "PACKAGING_FAILED"

        events = audit_events(audit_table, pkg.user["user_id"], "package_workflow")
        assert len(events) == 1
        record = events[0]
        assert_audit_record(record, pkg.user["user_id"], pkg.workflow_id,
                            result="failure")
        assert record["details"]["failing_artifact"] == "plugins/x86_64/dda-dewarp.so"


class TestDeploymentAudit:
    """Audit writes for workflow deployment via deployments.handler
    (Requirement 11.5)."""

    @pytest.fixture
    def deploy_env(self, env, mods, monkeypatch):
        """A validated + packaged workflow version and patched Use_Case
        account clients so create_workflow_deployment runs to completion
        against moto DynamoDB only."""
        creator = env.make_user(role="DataScientist")
        operator = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        workflow_id = create_workflow(env, creator, usecase_id)
        mark_version_validated(
            env, workflow_id,
            component_arn=("arn:aws:greengrass:us-east-1:123456789012:"
                           f"components:dda.workflow.{workflow_id}:versions:1.0.0"))

        greengrass = MagicMock(name="greengrassv2")
        greengrass.create_deployment.return_value = {
            "deploymentId": f"gg-dep-{uuid.uuid4()}",
            "iotJobId": "job-1",
            "iotJobArn": "arn:aws:iot:us-east-1:123456789012:job/job-1",
        }
        iot = MagicMock(name="iot")

        def fake_get_usecase_client(service_name, usecase, session_name=None,
                                    region=None):
            return {"greengrassv2": greengrass, "iot": iot}[service_name]

        monkeypatch.setattr(
            mods.deployments, "get_usecase_client", fake_get_usecase_client)
        # No existing deployment for the target; all devices compatible.
        monkeypatch.setattr(
            mods.deployments, "find_latest_deployment_for_target",
            lambda client, target_arn: None)
        monkeypatch.setattr(
            mods.deployments, "check_local_server_compatibility",
            lambda client, things, min_version: [])

        def deploy(user):
            event = env.event("POST", "/deployments", user, body={
                "component_type": "workflow",
                "usecase_id": usecase_id,
                "workflow_id": workflow_id,
                "workflow_version": 1,
                "target_devices": ["device-1"],
            })
            response = mods.deployments.handler(event, None)
            return response["statusCode"], json.loads(response["body"])

        return SimpleNamespace(
            operator=operator, usecase_id=usecase_id,
            workflow_id=workflow_id, greengrass=greengrass, deploy=deploy)

    def test_deploy_writes_audit_record(self, env, deploy_env, audit_table):
        """A successful workflow deployment records deploy_workflow with
        the acting user, deployment id, and timestamp (Requirement 11.5)."""
        status, payload = deploy_env.deploy(deploy_env.operator)
        assert status == 201, payload
        deployment_id = payload["deployment_id"]

        events = audit_events(
            audit_table, deploy_env.operator["user_id"], "deploy_workflow")
        assert len(events) == 1
        record = events[0]
        assert_audit_record(record, deploy_env.operator["user_id"],
                            deploy_env.workflow_id)
        assert record["details"]["usecase_id"] == deploy_env.usecase_id
        assert int(record["details"]["workflow_version"]) == 1
        assert record["details"]["deployment_id"] == deployment_id
        assert record["details"]["target_devices"] == ["device-1"]

    def test_denied_deploy_writes_unauthorized_access_record(
            self, env, deploy_env, audit_table):
        """A denied deployment attempt records unauthorized_access with
        result denied (Requirement 11.4)."""
        viewer = env.make_user(role="Viewer")
        status, payload = deploy_env.deploy(viewer)
        assert status == 403
        assert payload["error"]["code"] == "FORBIDDEN"

        events = audit_events(
            audit_table, viewer["user_id"], "unauthorized_access")
        assert len(events) == 1
        record = events[0]
        assert record["result"] == "denied"
        assert "workflow:deploy" in record["details"]["required_permissions"]
        assert record["details"]["operation"] == "deploy_workflow"
        # No deployment was created and no success audit written.
        deploy_env.greengrass.create_deployment.assert_not_called()
        assert audit_events(audit_table, viewer["user_id"], "deploy_workflow") == []
