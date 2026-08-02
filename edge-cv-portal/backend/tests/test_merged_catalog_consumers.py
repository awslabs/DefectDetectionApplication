"""
Merged Node_Type_Catalog resolution wiring (custom-node-designer task 9.2).

Covers the existing catalog consumers against the moto stack from
conftest.py plus the pure merge/marker/exclusion logic in
functions/node_catalog_resolution.py:

1. GET /workflows/node-catalog (workflow_validation.py): without
   ``usecase_id`` the built-in catalog is served unchanged; with a
   Use_Case the registered Custom_Node_Types merge in — backing
   Plugin_Record Lifecycle_State test/prod only (dev excluded, 9.2),
   deprecated excluded from the palette (14.3), and test-state entries
   carrying ``lifecycleState: "test"`` (9.6). Unauthorized Use_Cases 403.
2. POST /workflows/{id}/validate validates against the merged catalog
   for the workflow's Use_Case, so custom nodes produce no unknown-type
   finding — including deprecated types (14.3).
3. Workflow save (workflows.py) records the Custom_Node_Type version
   used on the WorkflowVersions item as the ``custom_node_types`` map
   {typeId: typeVersion} (14.2), which the removal reference scan in
   custom_node_types.py honors. No inverted-index GSI is created (one
   scalar attribute cannot index multiple references per item).
4. workflow_generator.py embeds the merged palette catalog in the
   generation system prompt.
5. Pure helpers: palette entry selection, resolution pinning, and
   reference extraction.

_Requirements: 8.2, 8.3, 9.2, 9.6, 14.2, 14.3_
"""
import json
import sys
import uuid

import pytest

from conftest import TEST_ENV
from test_custom_node_types import NodeTypesEnv, make_declaration


@pytest.fixture
def nenv(aws_stack, monkeypatch):
    return NodeTypesEnv(aws_stack, monkeypatch)


@pytest.fixture(scope="module")
def validation_module(aws_stack):
    """functions/workflow_validation.py imported inside the moto stack."""
    sys.modules.pop("workflow_validation", None)
    import workflow_validation

    return workflow_validation


def set_lifecycle_state(nenv, plugin, state):
    """Force the backing Plugin_Record version's Lifecycle_State."""
    nenv.stack.tables.plugin_records.update_item(
        Key={"plugin_id": plugin["plugin_id"], "version": plugin["version"]},
        UpdateExpression="SET lifecycle_state = :s",
        ExpressionAttributeValues={":s": state},
    )


def register_custom_type(nenv, type_id, lifecycle_state="test",
                         built_archs=("x86_64",)):
    """A registered Custom_Node_Type whose backing plugin is in the
    given Lifecycle_State; returns (plugin, node_type_id)."""
    plugin = nenv.seed_plugin(built_archs=built_archs,
                              name=f"plg-{uuid.uuid4().hex[:8]}")
    status, body = nenv.register(nenv.admin, plugin,
                                 make_declaration(type_id, archs=built_archs))
    assert status == 201, body
    set_lifecycle_state(nenv, plugin, lifecycle_state)
    return plugin


def catalog_request(module, user, usecase_id=None):
    """GET /workflows/node-catalog[?usecase_id=...]; returns (status, body)."""
    event = {
        "httpMethod": "GET",
        "resource": "/workflows/node-catalog",
        "path": "/workflows/node-catalog",
        "queryStringParameters": (
            {"usecase_id": usecase_id} if usecase_id else None),
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
    response = module.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def by_type_id(body):
    return {n["typeId"]: n for n in body["nodeTypes"]}


def custom_workflow_definition(type_id):
    """csi_camera_source -> custom (VideoFrames in/out) -> capture."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "n1", "type": "csi_camera_source",
             "position": {"x": 100, "y": 100}, "parameters": {}},
            {"id": "n2", "type": type_id,
             "position": {"x": 350, "y": 100}, "parameters": {"radius": 5}},
            {"id": "n3", "type": "capture",
             "position": {"x": 600, "y": 100},
             "parameters": {"output_path": "/data/captures"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "n1", "port": "out"},
             "to": {"node": "n2", "port": "in"}},
            {"id": "c2", "from": {"node": "n2", "port": "out"},
             "to": {"node": "n3", "port": "in"}},
        ],
    }


def seed_stored_workflow(nenv, definition, custom_node_types=None):
    """A saved workflow (metadata + version item + S3 document) the
    validate endpoint can load."""
    workflow_id = f"wf-{uuid.uuid4()}"
    s3_key = (f"workflows/{nenv.usecase_id}/{workflow_id}/versions/1/"
              f"workflow.json")
    nenv.s3.put_object(Bucket=nenv.bucket, Key=s3_key,
                       Body=json.dumps(definition).encode("utf-8"))
    nenv.stack.tables.workflows.put_item(Item={
        "workflow_id": workflow_id,
        "usecase_id": nenv.usecase_id,
        "name": "merged-catalog-test",
        "latest_version": 1,
        "created_at": 1,
        "updated_at": 1,
    })
    nenv.stack.tables.versions.put_item(Item={
        "workflow_id": workflow_id,
        "version": 1,
        "s3_definition_key": s3_key,
        "validation_status": {"status": "none"},
        "custom_node_types": custom_node_types or {},
    })
    return workflow_id


def validate_request(module, nenv, user, workflow_id):
    event = {
        "httpMethod": "POST",
        "resource": "/workflows/{id}/validate",
        "path": f"/workflows/{workflow_id}/validate",
        "pathParameters": {"id": workflow_id},
        "body": json.dumps({}),
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
    response = module.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


# ===========================================================================
# 1. GET /workflows/node-catalog (8.2, 8.3, 9.2, 9.6, 14.3)
# ===========================================================================

class TestNodeCatalogMerge:

    def test_without_usecase_the_builtin_catalog_is_served_unchanged(
            self, nenv, validation_module):
        from workflow_core.catalog import NODE_CATALOG

        status, body = catalog_request(validation_module, nenv.admin)
        assert status == 200
        assert body["count"] == len(NODE_CATALOG)
        assert {n["typeId"] for n in body["nodeTypes"]} == \
            {d.type_id for d in NODE_CATALOG}
        assert all("lifecycleState" not in n for n in body["nodeTypes"])

    def test_test_state_custom_type_is_served_with_test_marker(
            self, nenv, validation_module):
        register_custom_type(nenv, "custom.merge_test", "test")

        status, body = catalog_request(validation_module, nenv.admin,
                                       nenv.usecase_id)
        assert status == 200
        entry = by_type_id(body)["custom.merge_test"]
        assert entry["lifecycleState"] == "test"          # 9.6
        assert entry["category"] == "preprocessing"       # 8.3
        # Same declaration structure as built-in types (8.2).
        assert entry["inputs"] == [{"name": "in", "portType": "VideoFrames"}]
        assert entry["outputs"] == [{"name": "out", "portType": "VideoFrames"}]
        # Built-in entries never carry a marker.
        assert "lifecycleState" not in by_type_id(body)["csi_camera_source"]

    def test_prod_state_custom_type_is_served_without_marker(
            self, nenv, validation_module):
        register_custom_type(nenv, "custom.merge_prod", "prod")

        status, body = catalog_request(validation_module, nenv.admin,
                                       nenv.usecase_id)
        assert status == 200
        entry = by_type_id(body)["custom.merge_prod"]
        assert "lifecycleState" not in entry

    def test_dev_state_custom_type_is_excluded(self, nenv, validation_module):
        register_custom_type(nenv, "custom.merge_dev", "dev")

        status, body = catalog_request(validation_module, nenv.admin,
                                       nenv.usecase_id)
        assert status == 200
        assert "custom.merge_dev" not in by_type_id(body)   # 9.2

    def test_deprecated_custom_type_is_excluded_from_the_palette(
            self, nenv, validation_module):
        register_custom_type(nenv, "custom.merge_deprecated", "prod")
        status, _ = nenv.deprecate(nenv.admin, "custom.merge_deprecated")
        assert status == 200

        status, body = catalog_request(validation_module, nenv.admin,
                                       nenv.usecase_id)
        assert status == 200
        assert "custom.merge_deprecated" not in by_type_id(body)   # 14.3

    def test_other_usecases_do_not_see_the_custom_type(
            self, nenv, env, validation_module):
        """Custom_Node_Types are scoped to their Use_Cases (8.2)."""
        register_custom_type(nenv, "custom.merge_scoped", "prod")

        other_usecase = env.create_usecase("Other Use Case")
        outsider = env.make_user()
        env.assign_role(outsider, other_usecase, "DataScientist")

        status, body = catalog_request(validation_module, outsider,
                                       other_usecase)
        assert status == 200
        assert "custom.merge_scoped" not in by_type_id(body)


# ===========================================================================
# 2. Validation against the merged catalog (14.2, 14.3)
# ===========================================================================

class TestValidationAgainstMergedCatalog:

    def test_workflow_with_registered_custom_node_validates_clean(
            self, nenv, validation_module):
        register_custom_type(nenv, "custom.merge_validate", "test")
        workflow_id = seed_stored_workflow(
            nenv, custom_workflow_definition("custom.merge_validate"),
            custom_node_types={"custom.merge_validate": 1})

        status, body = validate_request(validation_module, nenv,
                                        nenv.admin, workflow_id)
        assert status == 200, body
        assert body["error_count"] == 0, body["findings"]
        assert not any(f["code"] == "UNKNOWN_NODE_TYPE"
                       for f in body["findings"])

    def test_deprecated_custom_node_still_validates(self, nenv,
                                                    validation_module):
        """Deprecated types stay resolvable for existing workflows (14.3)."""
        register_custom_type(nenv, "custom.merge_val_deprecated", "prod")
        workflow_id = seed_stored_workflow(
            nenv, custom_workflow_definition("custom.merge_val_deprecated"),
            custom_node_types={"custom.merge_val_deprecated": 1})
        status, _ = nenv.deprecate(nenv.admin, "custom.merge_val_deprecated")
        assert status == 200

        status, body = validate_request(validation_module, nenv,
                                        nenv.admin, workflow_id)
        assert status == 200, body
        assert body["error_count"] == 0, body["findings"]

    def test_unregistered_custom_node_is_an_unknown_type(self, nenv,
                                                         validation_module):
        workflow_id = seed_stored_workflow(
            nenv, custom_workflow_definition("custom.never_registered"))

        status, body = validate_request(validation_module, nenv,
                                        nenv.admin, workflow_id)
        assert status == 200, body
        assert any(f["code"] == "UNKNOWN_NODE_TYPE" and f["nodeId"] == "n2"
                   for f in body["findings"])


# ===========================================================================
# 3. Workflow save records the Custom_Node_Type version (14.2)
# ===========================================================================

class TestSaveRecordsCustomNodeTypeVersions:

    def _save(self, nenv, user, definition, name="merged-save-test"):
        event = {
            "httpMethod": "POST",
            "resource": "/workflows",
            "path": "/workflows",
            "pathParameters": None,
            "queryStringParameters": None,
            "body": json.dumps({"usecase_id": nenv.usecase_id,
                                "name": name, "definition": definition}),
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
        response = nenv.stack.workflows.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def _version_item(self, nenv, workflow_id, version):
        response = nenv.stack.tables.versions.get_item(
            Key={"workflow_id": workflow_id, "version": version})
        return response["Item"]

    def test_save_records_the_custom_node_type_version_map(self, nenv):
        register_custom_type(nenv, "custom.merge_save", "test")

        status, body = self._save(
            nenv, nenv.admin, custom_workflow_definition("custom.merge_save"))
        assert status == 201, body

        item = self._version_item(nenv, body["workflow"]["workflow_id"], 1)
        assert item["custom_node_types"] == {"custom.merge_save": 1}

    def test_save_records_the_latest_registered_version(self, nenv):
        plugin = register_custom_type(nenv, "custom.merge_save_v2", "test")
        # A declaration update creates version 2 (14.1); a save afterwards
        # pins version 2 (14.2: the version used).
        status, _ = nenv.update(
            nenv.admin, "custom.merge_save_v2",
            declaration=make_declaration("custom.merge_save_v2"),
            plugin_version=plugin["version"])
        assert status == 201

        status, body = self._save(
            nenv, nenv.admin,
            custom_workflow_definition("custom.merge_save_v2"),
            name="merged-save-v2")
        assert status == 201, body

        item = self._version_item(nenv, body["workflow"]["workflow_id"], 1)
        assert item["custom_node_types"] == {"custom.merge_save_v2": 2}

    def test_builtin_only_save_records_an_empty_map(self, nenv):
        definition = {
            "schemaVersion": 1,
            "nodes": [
                {"id": "n1", "type": "csi_camera_source",
                 "position": {"x": 100, "y": 100}, "parameters": {}},
                {"id": "n2", "type": "capture",
                 "position": {"x": 350, "y": 100},
                 "parameters": {"output_path": "/data/captures"}},
            ],
            "connections": [
                {"id": "c1", "from": {"node": "n1", "port": "out"},
                 "to": {"node": "n2", "port": "in"}},
            ],
        }
        status, body = self._save(nenv, nenv.admin, definition,
                                  name="builtin-only")
        assert status == 201, body

        item = self._version_item(nenv, body["workflow"]["workflow_id"], 1)
        assert item["custom_node_types"] == {}

    def test_recorded_references_block_custom_node_type_removal(self, nenv):
        """The recorded map is what the removal reference scan honors
        (14.4/14.5 wiring: no GSI, map attribute at save)."""
        register_custom_type(nenv, "custom.merge_save_refs", "test")
        status, body = self._save(
            nenv, nenv.admin,
            custom_workflow_definition("custom.merge_save_refs"),
            name="merged-save-refs")
        assert status == 201, body
        workflow_id = body["workflow"]["workflow_id"]

        nenv.patch_usecase_clients()
        status, body = nenv.remove(nenv.admin, "custom.merge_save_refs")
        assert status == 409
        assert body["error"]["code"] == "CUSTOM_NODE_TYPE_IN_USE"
        referencing = body["error"]["details"]["referencing_workflows"]
        assert {"workflow_id": workflow_id, "version": 1} in referencing


# ===========================================================================
# 4. Generation system prompt embeds the merged catalog
# ===========================================================================

class TestGeneratorMergedCatalog:

    def test_system_prompt_embeds_registered_custom_types(self, nenv):
        sys.modules.pop("workflow_generator", None)
        import workflow_generator

        register_custom_type(nenv, "custom.merge_generate", "test")

        catalog, _ = workflow_generator.palette_catalog_for_usecase(
            nenv.usecase_id)
        prompt = workflow_generator.build_system_prompt(catalog)
        assert '"custom.merge_generate"' in prompt
        # Built-ins remain embedded alongside the custom types.
        assert '"csi_camera_source"' in prompt

    def test_default_prompt_stays_builtin_only(self, nenv):
        sys.modules.pop("workflow_generator", None)
        import workflow_generator

        prompt = workflow_generator.build_system_prompt()
        assert workflow_generator.serialized_catalog_json() in prompt


# ===========================================================================
# 5. Pure merge/marker/exclusion logic (tasks 9.3/9.4 property-test these)
# ===========================================================================

def make_item(type_id, version=1, plugin_id="p1", plugin_version=1,
              deprecated=False):
    return {
        "node_type_id": type_id,
        "version": version,
        "usecase_id": "uc-1",
        "usecase_ids": ["uc-1"],
        "plugin_id": plugin_id,
        "plugin_version": plugin_version,
        "declaration": make_declaration(type_id),
        "deprecated": deprecated,
    }


class TestPureResolutionLogic:

    def test_palette_entries_filter_lifecycle_and_deprecation(self):
        from node_catalog_resolution import palette_entries

        items = [
            make_item("custom.dev", plugin_id="pd"),
            make_item("custom.test", plugin_id="pt"),
            make_item("custom.prod", plugin_id="pp"),
            make_item("custom.deprecated", plugin_id="px", deprecated=True),
            make_item("custom.unknown_state", plugin_id="pu"),
        ]
        states = {("pd", 1): "dev", ("pt", 1): "test", ("pp", 1): "prod",
                  ("px", 1): "prod"}

        entries = palette_entries(items, states)
        assert [(i["node_type_id"], m) for i, m in entries] == [
            ("custom.prod", None),
            ("custom.test", "test"),
        ]

    def test_palette_serves_only_the_latest_version(self):
        from node_catalog_resolution import palette_entries

        items = [make_item("custom.v", version=1),
                 make_item("custom.v", version=3),
                 make_item("custom.v", version=2)]
        entries = palette_entries(items, {("p1", 1): "prod"})
        assert [(i["node_type_id"], i["version"]) for i, _ in entries] == \
            [("custom.v", 3)]

    def test_resolution_honors_pinned_versions(self):
        from node_catalog_resolution import resolution_items

        items = [make_item("custom.pin", version=1),
                 make_item("custom.pin", version=2)]
        assert [i["version"] for i in resolution_items(items)] == [2]
        assert [i["version"]
                for i in resolution_items(items, {"custom.pin": 1})] == [1]
        # A pin to a vanished version falls back to the latest.
        assert [i["version"]
                for i in resolution_items(items, {"custom.pin": 9})] == [2]

    def test_resolution_keeps_deprecated_and_dev_types(self):
        from node_catalog_resolution import resolution_items

        items = [make_item("custom.gone", deprecated=True)]
        assert [i["node_type_id"] for i in resolution_items(items)] == \
            ["custom.gone"]

    def test_referenced_node_type_versions_ignores_builtins(self):
        from node_catalog_resolution import referenced_node_type_versions

        items = [make_item("custom.ref", version=4)]
        definition = custom_workflow_definition("custom.ref")
        assert referenced_node_type_versions(definition, items) == \
            {"custom.ref": 4}

    def test_referenced_node_type_versions_skips_unregistered_types(self):
        from node_catalog_resolution import referenced_node_type_versions

        definition = custom_workflow_definition("custom.unregistered")
        assert referenced_node_type_versions(definition, []) == {}

    def test_builtins_win_on_type_id_collision(self):
        from node_catalog_resolution import resolve_palette_catalog
        from workflow_core.catalog import NODE_CATALOG

        impostor = make_item("csi_camera_source")
        merged, markers = resolve_palette_catalog(
            [impostor], {("p1", 1): "test"})
        assert merged == NODE_CATALOG
        assert markers == {}
