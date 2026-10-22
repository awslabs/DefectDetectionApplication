"""Catalog machinery of the Portal_MCP_Server (`mcp_portal/catalog.py`).

Spec: .kiro/specs/mcp-servers/ (design.md "2. `mcp_portal` package"
`catalog.py` row, "Tool catalogs" conventions, D8, D35, Input validation;
tasks.md 4.1). Requirements 1.2, 1.7, 3.8, 5.1, 5.3, 5.4, 14.1, 14.2, 14.4,
17.3.

Rendering, alias entries, `confirm` branch rendering and schema generation,
on synthetic specs (the real rows arrive with tasks 5.1-5.4).
"""
import json
import re

import jsonschema
import pytest

from mcp_portal import catalog as cat
from mcp_portal.catalog import (LIMIT, NEXT_TOKEN, OFFSET, PAGE, AcceptedWrite,
                                ToolSpec, identifier, schema, text)


def spec(name="portal_devices_thing_get", **overrides):
    values = dict(
        name=name, domain="devices", title="Get a thing",
        description="Returns one thing.", resource_type="device",
        input_schema=schema({"usecase_id": identifier(),
                             "device_id": identifier()},
                            required=("usecase_id", "device_id")),
        backend="devices", handler_module="devices",
        handler_function="get_device", method="GET",
        resource="/devices/{id}", param_map={"usecase_id": "usecase_id"},
        permissions=("view_devices",), scope="usecase")
    values.update(overrides)
    return ToolSpec(**values)


def list_spec(**overrides):
    values = dict(
        name="portal_devices_things_list", title="List things",
        description="Lists things.", resource="/devices",
        handler_function="list_devices", result_shape="collection",
        collection_key="devices",
        input_schema=schema({"usecase_id": identifier(), "limit": LIMIT,
                             "offset": OFFSET}, required=("usecase_id",)))
    values.update(overrides)
    return spec(**values)


def mutating(**overrides):
    values = dict(name="portal_devices_thing_delete", method="DELETE",
                  classification="mutating", destructive=True,
                  idempotent=False, confirm_target="device_id",
                  status_tool="portal_devices_things_list", phase=3)
    values.update(overrides)
    return spec(**values)


# ------------------------------------------------------------- constants

def test_server_consumed_args_and_note():
    assert cat.SERVER_CONSUMED_ARGS == ("confirm", "usecase_id", "limit", "offset")
    assert cat.STATUS_SYNC_NOTE.startswith("status sync: the GET refreshes")
    assert cat.STATUS_SYNC_NOTE.endswith("nothing the caller supplied is persisted")
    assert cat.CATALOG == () or all(isinstance(s, ToolSpec) for s in cat.CATALOG)


def test_path_parameter_pattern():
    pattern = re.compile(cat.PATH_PARAM_PATTERN)
    for good in ("dev-1", "a.b", "x:y@z+w=", "...", "a" * 256):
        assert pattern.match(good), good
    for bad in (".", "..", "a/b", "", "a" * 257, "a b", "static-image/pin"):
        assert not pattern.match(bad), bad


# --------------------------------------------------------------- rendering

def test_read_only_description():
    rendered = cat.render_description(spec())
    assert rendered == ("Returns one thing. Touches: device in the Use_Case "
                        "named by usecase_id. Read-only.")
    assert rendered.endswith("Read-only.")


def test_global_spec_has_no_usecase_text():
    global_spec = spec(scope="global", input_schema=schema(
        {"device_id": identifier()}, required=("device_id",)), param_map={})
    assert global_spec.usecase_arg is None
    assert cat.render_description(global_spec) == (
        "Returns one thing. Touches: device. Read-only.")


def test_mutating_description():
    updated = mutating(destructive=False, confirm_target=None,
                       name="portal_devices_thing_update", method="PUT")
    assert cat.classification_text(updated) == "MUTATING."
    assert cat.render_description(updated).endswith(". MUTATING.")


def test_destructive_named_target():
    assert cat.classification_text(mutating()) == (
        "MUTATING, DESTRUCTIVE — requires confirm=device_id.")


def test_destructive_literal():
    literal = mutating(confirm_target=None, confirm_literal="restart-all")
    assert cat.render_description(literal).endswith(
        "MUTATING, DESTRUCTIVE — requires confirm='restart-all'.")


def test_destructive_per_branch_tuple():
    deployment = mutating(
        name="portal_devices_deployment_create", method="POST",
        resource="/deployments", param_map={"usecase_id": "usecase_id"},
        input_schema=schema(
            {"usecase_id": identifier(),
             "target_thing_group": identifier(),
             "target_devices": {"type": "array", "items": identifier(),
                                "minItems": 1, "maxItems": 1},
             "confirm": text(256)},
            required=("usecase_id", "confirm"),
            one_of=(("target_thing_group",), ("target_devices",))),
        confirm_target=["target_thing_group", "target_devices[0]"])
    assert deployment.confirm_target == ("target_thing_group", "target_devices[0]")
    assert cat.classification_text(deployment) == (
        "MUTATING, DESTRUCTIVE — requires "
        "confirm=<target_thing_group | target_devices[0]>.")
    assert deployment.input_schema["oneOf"] == [
        {"required": ["target_thing_group"]}, {"required": ["target_devices"]}]


def test_to_mcp_tool_annotations():
    tool = cat.to_mcp_tool(mutating())
    assert tool.name == "portal_devices_thing_delete"
    assert tool.title == "Get a thing"
    assert tool.description == cat.render_description(mutating())
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is True
    assert tool.annotations.idempotent_hint is False
    assert tool.annotations.open_world_hint is False
    dumped = tool.model_dump(by_alias=True, exclude_none=True)
    assert dumped["inputSchema"]["additionalProperties"] is False
    read = cat.to_mcp_tool(spec()).annotations
    assert (read.read_only_hint, read.destructive_hint, read.open_world_hint) == (
        True, False, False)


# ---------------------------------------------------------------- listing

def _catalog():
    return (
        spec(name="portal_devices_zeta_get"),
        spec(name="portal_devices_alpha_get", deprecated_alias="portal_devices_old_get",
             alias_removed_in="2.0.0"),
        mutating(),
        spec(name="portal_devices_later_get", phase=2),
        spec(name="portal_admin_thing_get", domain="admin", resource_type="collection"),
        spec(name="portal_models_thing_get", domain="models", resource_type="model",
             deprecated_alias="portal_models_zz_get", alias_removed_in="1.4.0"),
    )


def names(tools):
    return [tool.name for tool in tools]


def test_tools_for_view_filters_and_sorts():
    tools = cat.tools_for_view("devices", (1,), False, catalog=_catalog())
    assert names(tools) == ["portal_devices_alpha_get", "portal_devices_old_get",
                            "portal_devices_zeta_get"]


def test_mutating_switch_and_phases():
    assert "portal_devices_thing_delete" not in names(
        cat.tools_for_view("devices", (1, 2, 3), False, catalog=_catalog()))
    on = names(cat.tools_for_view("devices", (1, 2, 3), True, catalog=_catalog()))
    assert "portal_devices_thing_delete" in on
    assert "portal_devices_later_get" in on
    assert on == sorted(on)
    assert "portal_devices_thing_delete" not in names(
        cat.tools_for_view("devices", (1, 2), True, catalog=_catalog()))


def test_aggregate_view_sorted_by_domain_then_name():
    tools = cat.tools_for_view(None, (1,), False, catalog=_catalog())
    assert names(tools) == [
        "portal_admin_thing_get",
        "portal_devices_alpha_get", "portal_devices_old_get",
        "portal_devices_zeta_get",
        "portal_models_thing_get", "portal_models_zz_get"]


def test_alias_entry():
    tools = {t.name: t for t in cat.tools_for_view(None, (1,), False,
                                                   catalog=_catalog())}
    alias, target = tools["portal_devices_old_get"], tools["portal_devices_alpha_get"]
    assert alias.description == (
        "Deprecated alias of portal_devices_alpha_get; removed in 2.0.0. "
        + target.description)
    assert alias.input_schema == target.input_schema
    assert alias.annotations == target.annotations
    assert alias.title == target.title


def test_alias_listed_iff_target_listed():
    hidden = (spec(name="portal_devices_alpha_get", phase=2,
                   deprecated_alias="portal_devices_old_get",
                   alias_removed_in="2.0.0"),)
    assert cat.tools_for_view("devices", (1,), True, catalog=hidden) == []


def test_listing_is_deterministic():
    first = cat.tools_for_view(None, (1, 2, 3), True, catalog=_catalog())
    second = cat.tools_for_view(None, (1, 2, 3), True, catalog=_catalog())
    assert [t.model_dump() for t in first] == [t.model_dump() for t in second]


def test_unknown_view_rejected():
    with pytest.raises(ValueError):
        cat.tools_for_view("nope", (1,), False, catalog=_catalog())


# ------------------------------------------------------- schema generation

def test_limit_and_offset_defaults():
    properties = list_spec().input_schema["properties"]
    assert properties["limit"] == {"type": "integer", "minimum": 1,
                                   "maximum": 200, "default": 50}
    assert properties["offset"] == {"type": "integer", "minimum": 0, "default": 0}


def test_narrowed_limit():
    narrowed = list_spec(limit_max=30, limit_default=20)
    assert narrowed.input_schema["properties"]["limit"] == {
        "type": "integer", "minimum": 1, "maximum": 30, "default": 20}
    only_default = list_spec(limit_default=20)
    assert only_default.input_schema["properties"]["limit"]["maximum"] == 200
    assert only_default.input_schema["properties"]["limit"]["default"] == 20


def test_page_and_next_token():
    paged = list_spec(input_schema=schema(
        {"usecase_id": identifier(), "limit": LIMIT, "page": PAGE,
         "next_token": dict(NEXT_TOKEN, description="cursor")},
        required=("usecase_id",)))
    properties = paged.input_schema["properties"]
    assert properties["page"] == {"type": "integer", "minimum": 1, "default": 1}
    assert properties["next_token"] == {"type": "string", "maxLength": 2048,
                                        "description": "cursor"}


def test_generation_does_not_mutate_the_input():
    raw = schema({"usecase_id": identifier(), "limit": LIMIT})
    snapshot = json.dumps(raw, sort_keys=True)
    list_spec(input_schema=raw)
    assert json.dumps(raw, sort_keys=True) == snapshot
    assert LIMIT == {"type": "integer"}


def test_generated_schemas_are_valid_draft_2020_12():
    for candidate in (spec(), list_spec(), mutating()):
        jsonschema.Draft202012Validator.check_schema(candidate.input_schema)
        assert candidate.input_schema["additionalProperties"] is False


def test_path_pattern_on_every_path_argument():
    explicit = spec(name="portal_admin_usecase_get", domain="admin",
                    resource="/usecases/{id}", input_schema=schema(
                        {"usecase_id": identifier()}, required=("usecase_id",)),
                    param_map={"usecase_id": "id"})
    assert explicit.input_schema["properties"]["usecase_id"]["pattern"] == \
        cat.PATH_PARAM_PATTERN
    bare = spec()
    assert bare.param_map == {"usecase_id": "usecase_id", "device_id": "id"}
    assert bare.input_schema["properties"]["device_id"]["pattern"] == \
        cat.PATH_PARAM_PATTERN
    # usecase_id travels in the query: the general identifier pattern.
    assert bare.input_schema["properties"]["usecase_id"]["pattern"] == \
        cat.IDENTIFIER_PATTERN
    validator = jsonschema.Draft202012Validator(bare.input_schema)
    for value in ("a/b", ".", ".."):
        assert not validator.is_valid({"usecase_id": "uc", "device_id": value})
    assert validator.is_valid({"usecase_id": "uc/with-slash", "device_id": "d-1"})


def test_narrower_authored_path_pattern_is_kept():
    narrowed = spec(input_schema=schema(
        {"usecase_id": identifier(),
         "device_id": {"type": "string", "pattern": cat.THING_NAME_PATTERN}},
        required=("usecase_id", "device_id")))
    prop = narrowed.input_schema["properties"]["device_id"]
    assert prop["pattern"] == cat.PATH_PARAM_PATTERN
    assert prop["allOf"] == [{"pattern": cat.THING_NAME_PATTERN}]
    validator = jsonschema.Draft202012Validator(narrowed.input_schema)
    assert validator.is_valid({"usecase_id": "uc", "device_id": "thing-1"})
    # '@' passes the path pattern but not the thing-name pattern.
    assert not validator.is_valid({"usecase_id": "uc", "device_id": "a@b"})


# --------------------------------------------------- bare-identifier rule

def test_own_name_and_camel_case():
    own = spec(name="portal_admin_user_permissions_get", domain="admin",
               resource="/users/{user_id}/permissions", scope="global",
               input_schema=schema({"user_id": identifier()}), param_map={})
    assert own.param_map == {"user_id": "user_id"}
    camel = spec(resource="/workflows/{workflowId}", scope="global", param_map={},
                 input_schema=schema({"workflow_id": identifier()}))
    assert camel.param_map == {"workflow_id": "workflowId"}


def test_single_remaining_identifier_takes_the_unmapped_parameter():
    two_params = spec(resource="/devices/{id}/cameras/{csid}",
                      input_schema=schema({"usecase_id": identifier(),
                                           "device_id": identifier(),
                                           "camera_id": identifier()}),
                      param_map={"usecase_id": "usecase_id", "camera_id": "csid"})
    assert two_params.param_map["device_id"] == "id"


def test_identifier_left_over_travels_under_its_own_name():
    extra = spec(input_schema=schema({"usecase_id": identifier(),
                                      "device_id": identifier(),
                                      "job_id": identifier()}),
                 param_map={"usecase_id": "usecase_id", "device_id": "id"})
    assert "job_id" not in extra.param_map
    assert extra.location_of("job_id") == "query"
    assert extra.input_schema["properties"]["job_id"]["pattern"] == \
        cat.IDENTIFIER_PATTERN


def test_two_bare_identifiers_claiming_one_parameter_raise():
    with pytest.raises(cat.CatalogError, match="would claim"):
        spec(name="portal_devices_camera_add", method="POST",
             resource="/devices/{id}/cameras",
             input_schema=schema({"usecase_id": identifier(),
                                  "device_id": identifier(),
                                  "camera_source_id": identifier()}))
    # The row's explicit body entry resolves it (revision 11, review-10 N2).
    resolved = spec(name="portal_devices_camera_add", method="POST",
                    resource="/devices/{id}/cameras",
                    input_schema=schema({"usecase_id": identifier(),
                                         "device_id": identifier(),
                                         "camera_source_id": identifier()}),
                    param_map={"usecase_id": "usecase_id",
                               "camera_source_id": "camera_source_id"})
    assert resolved.param_map["device_id"] == "id"
    assert resolved.location_of("camera_source_id") == "body"


def test_usecase_id_is_never_a_bare_identifier():
    with_usecase = spec(name="portal_admin_usecase_delete", domain="admin",
                        resource="/usecases/{id}",
                        input_schema=schema({"usecase_id": identifier()}),
                        param_map={})
    assert with_usecase.param_map == {}


# ------------------------------------------------------------ locations

def test_locations():
    assert spec().location_of("device_id") == "path"
    assert spec().location_of("usecase_id") == "query"
    post = spec(method="POST", param_map={"usecase_id": "usecase_id"})
    assert post.location_of("usecase_id") == "body"
    analyze = spec(method="POST", param_map={"usecase_id": "usecase_id"},
                   param_in={"usecase_id": "query"})
    assert analyze.location_of("usecase_id") == "query"


# --------------------------------------------------------------- export

def _export_catalog():
    whoami = spec(name="portal_admin_whoami", domain="admin", backend=None,
                  handler_module="server", handler_function="whoami",
                  method=None, resource=None, scope="global", param_map={},
                  input_schema=schema())
    return _catalog() + (whoami, spec(name="portal_devices_dup_get"))


def test_catalog_routes_sorted_and_deduplicated():
    routes = cat.catalog_routes(_export_catalog())
    assert routes == [
        {"method": "DELETE", "resource": "/devices/{id}", "backend": "devices"},
        {"method": "GET", "resource": "/devices/{id}", "backend": "devices"},
    ]


def test_catalog_tools_and_export(tmp_path):
    tools = cat.catalog_tools(_export_catalog())
    assert [t["name"] for t in tools] == sorted(t["name"] for t in tools)
    delete = next(t for t in tools if t["name"] == "portal_devices_thing_delete")
    assert delete == {"name": "portal_devices_thing_delete",
                      "classification": "mutating", "destructive": True,
                      "phase": 3, "status_tool": "portal_devices_things_list"}
    routes_file, tools_file = tmp_path / "routes.json", tmp_path / "tools.json"
    cat.export(str(routes_file), str(tools_file), catalog=_export_catalog())
    assert json.loads(routes_file.read_text()) == cat.catalog_routes(_export_catalog())
    assert json.loads(tools_file.read_text()) == tools
    assert routes_file.read_text().endswith("]\n")


def test_export_command_usage(capsys):
    assert cat.main(["--bogus"]) == 2
    assert "--export-routes" in capsys.readouterr().err


def test_accepted_write_tuple():
    write = AcceptedWrite("models.get_model", "update_item", "table", 2)
    assert (write.function, write.call, write.receiver, write.count) == (
        "models.get_model", "update_item", "table", 2)
    synced = spec(accepted_writes=[write], accepted_writes_note=cat.STATUS_SYNC_NOTE)
    assert synced.accepted_writes == (write,)
