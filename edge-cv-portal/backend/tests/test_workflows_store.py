"""
Integration tests for the Workflow_Store API (functions/workflows.py).

Task 6.4 (spec: workflow-manager). CRUD, versioning, duplication, and
delete-with-deployments run against local DynamoDB / S3 (moto) with the
real shared_utils RBAC layer and the real workflow_core serializer.
_Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_
"""
import json

import pytest


def make_definition(topic="line-1/results"):
    """A minimal valid Workflow_Definition in canonical field/order form."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {
                "id": "cam",
                "type": "camera_source",
                "position": {"x": 0, "y": 0},
                "parameters": {},
            },
            {
                "id": "out",
                "type": "mqtt_publish",
                "position": {"x": 400, "y": 120},
                "parameters": {"topic": topic},
            },
        ],
        "connections": [
            {
                "id": "c1",
                "from": {"node": "cam", "port": "video"},
                "to": {"node": "out", "port": "in"},
            }
        ],
    }


def create_workflow(env, user, usecase_id, name="Line inspection", **overrides):
    body = {
        "usecase_id": usecase_id,
        "name": name,
        "description": "test workflow",
        "definition": make_definition(),
    }
    body.update(overrides)
    status, payload = env.invoke("POST", "/workflows", user, body=body)
    assert status == 201, payload
    return payload["workflow"]


# --------------------------------------------------------------------- 5.1
class TestCreate:
    def test_create_persists_scoped_metadata_and_definition(self, env):
        """Saving a workflow persists it scoped to account + Use_Case with
        name, description, and timestamps (Req 5.1)."""
        user = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()

        workflow = create_workflow(env, user, usecase_id)

        assert workflow["usecase_id"] == usecase_id
        assert workflow["account_id"] == "123456789012"
        assert workflow["name"] == "Line inspection"
        assert workflow["description"] == "test workflow"
        assert workflow["latest_version"] == 1
        assert workflow["created_by"] == user["user_id"]
        assert workflow["created_at"] == workflow["updated_at"]
        assert isinstance(workflow["created_at"], int)

        # The definition document is stored in portal S3 under the
        # per-usecase / per-workflow prefix.
        key = (
            f"workflows/{usecase_id}/{workflow['workflow_id']}"
            f"/versions/1/workflow.json"
        )
        stored = json.loads(
            env.s3.get_object(Bucket=env.bucket, Key=key)["Body"].read()
        )
        assert stored["schemaVersion"] == 1
        assert {n["id"] for n in stored["nodes"]} == {"cam", "out"}

    def test_create_missing_fields_rejected(self, env):
        user = env.make_user(role="DataScientist")
        status, payload = env.invoke(
            "POST", "/workflows", user, body={"name": "no usecase or definition"}
        )
        assert status == 400
        assert payload["error"]["code"] == "MISSING_FIELDS"

    def test_create_invalid_definition_rejected(self, env):
        """A schema-violating definition is rejected with the serializer's
        descriptive error (Req 5.1 stores only valid definitions)."""
        user = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        status, payload = env.invoke(
            "POST", "/workflows", user,
            body={
                "usecase_id": usecase_id,
                "name": "bad",
                "definition": {"schemaVersion": 1},  # missing nodes/connections
            },
        )
        assert status == 400
        assert payload["error"]["code"] == "SCHEMA_VIOLATION"


# --------------------------------------------------------------------- 5.2
class TestVersioning:
    def test_save_changes_creates_new_version_and_retains_prior(self, env):
        """Saving changes creates a new version; prior versions stay
        loadable with their original contents (Req 5.2)."""
        user = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        workflow = create_workflow(env, user, usecase_id)
        workflow_id = workflow["workflow_id"]
        v1_definition = make_definition()
        v2_definition = make_definition(topic="line-2/results")

        status, payload = env.invoke(
            "PUT", "/workflows/{id}", user, workflow_id=workflow_id,
            body={"definition": v2_definition, "description": "revised"},
        )
        assert status == 200, payload
        assert payload["version"] == 2
        assert payload["workflow"]["latest_version"] == 2
        assert payload["workflow"]["description"] == "revised"

        # Version 1 is retained with its original definition.
        status, payload = env.invoke(
            "GET", "/workflows/{id}", user, workflow_id=workflow_id,
            query={"version": "1"},
        )
        assert status == 200
        assert payload["version"] == 1
        assert payload["definition"] == v1_definition

        # Latest returns the new definition.
        status, payload = env.invoke(
            "GET", "/workflows/{id}", user, workflow_id=workflow_id
        )
        assert status == 200
        assert payload["version"] == 2
        assert payload["definition"] == v2_definition

    def test_version_history_lists_all_versions_newest_first(self, env):
        user = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        workflow_id = create_workflow(env, user, usecase_id)["workflow_id"]
        for topic in ("t2", "t3"):
            status, _ = env.invoke(
                "PUT", "/workflows/{id}", user, workflow_id=workflow_id,
                body={"definition": make_definition(topic=topic)},
            )
            assert status == 200

        status, payload = env.invoke(
            "GET", "/workflows/{id}/versions", user, workflow_id=workflow_id
        )
        assert status == 200
        assert payload["latest_version"] == 3
        assert [v["version"] for v in payload["versions"]] == [3, 2, 1]


# --------------------------------------------------------------------- 5.3
class TestList:
    def test_list_returns_only_authorized_usecases(self, env):
        """Listing returns workflows of Use_Cases the user is assigned to,
        not workflows of other Use_Cases (Req 5.3)."""
        creator = env.make_user(role="DataScientist")
        usecase_a = env.create_usecase("A")
        usecase_b = env.create_usecase("B")
        wf_a = create_workflow(env, creator, usecase_a, name="wf-a")
        create_workflow(env, creator, usecase_b, name="wf-b")

        member = env.make_user(role="DataScientist")
        env.assign_role(member, usecase_a, "DataScientist")

        status, payload = env.invoke("GET", "/workflows", member)
        assert status == 200
        listed_ids = {w["workflow_id"] for w in payload["workflows"]}
        assert listed_ids == {wf_a["workflow_id"]}
        assert payload["count"] == 1

    def test_list_scoped_to_single_usecase_via_query(self, env):
        user = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        created = {
            create_workflow(env, user, usecase_id, name=f"wf-{i}")["workflow_id"]
            for i in range(2)
        }
        status, payload = env.invoke(
            "GET", "/workflows", user, query={"usecase_id": usecase_id}
        )
        assert status == 200
        assert {w["workflow_id"] for w in payload["workflows"]} == created


# --------------------------------------------------------------------- 5.4
class TestOpen:
    def test_open_returns_definition_exactly_as_saved(self, env):
        """Opening a saved workflow returns the stored definition with all
        nodes, positions, configurations, and connections (Req 5.4)."""
        user = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        definition = make_definition()
        workflow_id = create_workflow(env, user, usecase_id)["workflow_id"]

        status, payload = env.invoke(
            "GET", "/workflows/{id}", user, workflow_id=workflow_id
        )
        assert status == 200
        assert payload["workflow"]["workflow_id"] == workflow_id
        assert payload["definition"] == definition
        # positions and parameters survive the round trip untouched
        nodes = {n["id"]: n for n in payload["definition"]["nodes"]}
        assert nodes["out"]["position"] == {"x": 400, "y": 120}
        assert nodes["out"]["parameters"] == {"topic": "line-1/results"}

    def test_open_missing_workflow_returns_404(self, env):
        user = env.make_user(role="DataScientist")
        status, payload = env.invoke(
            "GET", "/workflows/{id}", user, workflow_id="does-not-exist"
        )
        assert status == 404
        assert payload["error"]["code"] == "WORKFLOW_NOT_FOUND"


# ---------------------------------------------------------------- 5.5, 5.6
class TestDelete:
    def test_delete_removes_workflow_versions_and_documents(self, env):
        """Deleting a workflow with no active deployments removes the
        metadata, every version record, and stored documents (Req 5.5)."""
        user = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        workflow_id = create_workflow(env, user, usecase_id)["workflow_id"]
        status, _ = env.invoke(
            "PUT", "/workflows/{id}", user, workflow_id=workflow_id,
            body={"definition": make_definition(topic="t2")},
        )
        assert status == 200

        status, payload = env.invoke(
            "DELETE", "/workflows/{id}", user, workflow_id=workflow_id
        )
        assert status == 200, payload

        # Workflow is gone from the API.
        status, _ = env.invoke(
            "GET", "/workflows/{id}", user, workflow_id=workflow_id
        )
        assert status == 404

        # All version records are gone.
        versions = env.stack.tables.versions.query(
            KeyConditionExpression="workflow_id = :wid",
            ExpressionAttributeValues={":wid": workflow_id},
        )["Items"]
        assert versions == []

        # All stored S3 documents are gone.
        listed = env.s3.list_objects_v2(
            Bucket=env.bucket,
            Prefix=f"workflows/{usecase_id}/{workflow_id}/",
        )
        assert listed.get("KeyCount", 0) == 0

    def test_delete_rejected_with_referencing_deployment_ids(self, env):
        """Deletion is rejected with 409 and the ids of active deployments
        that reference the workflow (Req 5.6)."""
        user = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        workflow_id = create_workflow(env, user, usecase_id)["workflow_id"]
        dep_assoc = env.put_deployment(
            usecase_id, status="IN_PROGRESS",
            component_type="workflow", workflow_id=workflow_id,
        )
        dep_component = env.put_deployment(
            usecase_id, status="ACTIVE",
            components=[{"component_name": f"dda.workflow.{workflow_id}"}],
        )
        # An inactive deployment must not block deletion.
        env.put_deployment(
            usecase_id, status="FAILED",
            component_type="workflow", workflow_id=workflow_id,
        )
        # An active deployment of a different workflow must not block.
        env.put_deployment(
            usecase_id, status="ACTIVE",
            component_type="workflow", workflow_id="other-workflow",
        )

        status, payload = env.invoke(
            "DELETE", "/workflows/{id}", user, workflow_id=workflow_id
        )
        assert status == 409
        assert payload["error"]["code"] == "WORKFLOW_HAS_ACTIVE_DEPLOYMENTS"
        assert sorted(payload["error"]["details"]["deployment_ids"]) == sorted(
            [dep_assoc, dep_component]
        )

        # The workflow and its versions remain intact.
        status, payload = env.invoke(
            "GET", "/workflows/{id}", user, workflow_id=workflow_id
        )
        assert status == 200
        assert payload["version"] == 1

    def test_delete_allowed_once_deployments_inactive(self, env):
        user = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        workflow_id = create_workflow(env, user, usecase_id)["workflow_id"]
        env.put_deployment(
            usecase_id, status="CANCELLED",
            component_type="workflow", workflow_id=workflow_id,
        )
        status, payload = env.invoke(
            "DELETE", "/workflows/{id}", user, workflow_id=workflow_id
        )
        assert status == 200, payload


# --------------------------------------------------------------------- 5.7
class TestDuplicate:
    def test_duplicate_creates_copy_under_new_name(self, env):
        """Duplicating creates a new workflow whose definition is a copy of
        the source's latest version, under a new name (Req 5.7)."""
        user = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        source = create_workflow(env, user, usecase_id, name="Original")
        source_id = source["workflow_id"]
        latest_definition = make_definition(topic="latest")
        status, _ = env.invoke(
            "PUT", "/workflows/{id}", user, workflow_id=source_id,
            body={"definition": latest_definition},
        )
        assert status == 200

        status, payload = env.invoke(
            "POST", "/workflows/{id}/duplicate", user, workflow_id=source_id,
            body={"name": "Copy of original"},
        )
        assert status == 201, payload
        copy = payload["workflow"]
        assert copy["workflow_id"] != source_id
        assert copy["name"] == "Copy of original"
        assert copy["usecase_id"] == usecase_id
        assert copy["latest_version"] == 1

        # Copy holds the source's latest definition.
        status, payload = env.invoke(
            "GET", "/workflows/{id}", user, workflow_id=copy["workflow_id"]
        )
        assert status == 200
        assert payload["definition"] == latest_definition

        # Source is unchanged.
        status, payload = env.invoke(
            "GET", "/workflows/{id}", user, workflow_id=source_id
        )
        assert status == 200
        assert payload["workflow"]["name"] == "Original"
        assert payload["version"] == 2

    def test_duplicate_default_name_appends_copy(self, env):
        user = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        source_id = create_workflow(env, user, usecase_id, name="Original")["workflow_id"]
        status, payload = env.invoke(
            "POST", "/workflows/{id}/duplicate", user, workflow_id=source_id, body={}
        )
        assert status == 201
        assert payload["workflow"]["name"] == "Original (copy)"


# ------------------------------------------------------------------- RBAC
class TestAuthorization:
    def test_viewer_cannot_create_workflow(self, env):
        """A role without workflow:create is denied with an authorization
        error (supports Req 5.3 scoping / 11.4)."""
        viewer = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        status, payload = env.invoke(
            "POST", "/workflows", viewer,
            body={
                "usecase_id": usecase_id,
                "name": "nope",
                "definition": make_definition(),
            },
        )
        assert status == 403
        assert payload["error"]["code"] == "FORBIDDEN"

    def test_viewer_cannot_delete_but_can_read(self, env):
        creator = env.make_user(role="DataScientist")
        usecase_id = env.create_usecase()
        workflow_id = create_workflow(env, creator, usecase_id)["workflow_id"]

        viewer = env.make_user(role="Viewer")
        status, payload = env.invoke(
            "DELETE", "/workflows/{id}", viewer, workflow_id=workflow_id
        )
        assert status == 403
        assert payload["error"]["code"] == "FORBIDDEN"

        status, _ = env.invoke(
            "GET", "/workflows/{id}", viewer, workflow_id=workflow_id
        )
        assert status == 200
