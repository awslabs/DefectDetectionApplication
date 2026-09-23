"""
Code assist diagnostics / multi-file extension unit tests
(custom-node-source-lifecycle task 8.5).

Covers the request 400 matrix for `diagnostics` and the extended
`context` (5.4, 5.6), the plugin_source contract happy path (5.9), the
target_file redirect to another Source_Tree file (5.7), INVALID_TARGET_FILE
for out-of-tree targets (5.8), hook targets still entry-point validated
(5.9), workflow-builder surfaces accepting `user` diagnostics (5.3), and
the scaffold-layout / build-platform system prompt (5.10, 7.4).

Reuses the fixtures of test_code_assist.py (moto settings table + mocked
Converse).
"""
from types import SimpleNamespace

import pytest

from test_code_assist import (  # noqa: F401 - fixtures
    TOOL_NAME, VALID_CODE, bedrock, clean_bedrock_config, ctx,
    forbid_bedrock, post_code_assist, request_body, tool_response,
)


@pytest.fixture(scope="module")
def ca(aws_stack):
    """Module-scoped twin of test_code_assist.ca: the settings table is
    created once per session (moto keeps it), and the code-assist modules
    are re-imported inside the mock so their bindings are fresh."""
    import os
    import sys
    from types import SimpleNamespace as _NS

    import boto3
    from botocore.exceptions import ClientError

    from conftest import REGION
    from test_code_assist import SETTINGS_TABLE_NAME

    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME
    try:
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=SETTINGS_TABLE_NAME,
            KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "setting_key",
                                   "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise
    for module_name in ("workflow_generator", "workflow_validation",
                        "code_assist", "bedrock_common"):
        sys.modules.pop(module_name, None)
    import workflow_generator
    import code_assist

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield _NS(generator=workflow_generator, code_assist=code_assist,
              settings_table=resource.Table(SETTINGS_TABLE_NAME))

HOOK = "plugin/frame_processing_hook.py"
C_SOURCE = "plugin/gstcustomblurregions.c"
MESON = "builds/arm64_jp6/meson.build"
TREE = {
    HOOK: "def process_frame(frame, params):\n    return frame\n",
    C_SOURCE: "#include <gst/gst.h>\n",
    MESON: "project('blur', 'c')\n",
    "README.md": "# blur\n",
}


def targeted_response(code, target_file, notes="Fixed."):
    response = tool_response(code, notes)
    response["output"]["message"]["content"][0]["toolUse"]["input"]["target_file"] = target_file
    return response


@pytest.fixture
def admin_ctx(env):
    """A UseCaseAdmin (node-designer:generate holder) on a fresh Use_Case."""
    usecase_id = env.create_usecase()
    user = env.make_user()
    env.assign_role(user, usecase_id, "UseCaseAdmin")
    return SimpleNamespace(usecase_id=usecase_id, user=user, env=env)


def designer_body(usecase_id, active=C_SOURCE, contract="plugin_source", **extra):
    files = {p: c for p, c in TREE.items() if p != active}
    body = request_body(
        usecase_id, surface="node-designer", contract=contract,
        prompt="Fix the build failure",
        current_code=TREE[active],
        context={"active_file": active, "files": files,
                 "file_paths": sorted(TREE), "kind": "scaffold",
                 "parameters": [{"name": "radius", "param_type": "int"}]},
    )
    body.update(extra)
    return body


# --------------------------------------------------------------- 400 matrix

class TestValidation:
    @pytest.mark.parametrize("diagnostics, detail", [
        ("not an object", "must be an object"),
        ({"kind": "compile", "text": "x"}, "kind"),
        ({"kind": "build", "architecture": "riscv", "text": "x"}, "architecture"),
        ({"kind": "build", "text": ""}, "non-empty"),
        ({"kind": "build", "text": "x" * (16 * 1024 + 1)}, "at most"),
    ])
    def test_invalid_diagnostics(self, ca, ctx, forbid_bedrock, diagnostics, detail):
        status, body = post_code_assist(
            ca, ctx.user, request_body(ctx.usecase_id, diagnostics=diagnostics))
        assert status == 400
        assert body["error"]["code"] == "INVALID_DIAGNOSTICS"
        assert detail in body["error"]["message"]

    @pytest.mark.parametrize("context, detail", [
        ({"files": ["a"]}, "context.files"),
        ({"files": {"a.c": 1}}, "context.files"),
        ({"files": {f"f{i}.c": "x" for i in range(65)}}, "at most 64"),
        ({"files": {"big.c": "x" * (256 * 1024 + 1)}}, "size limit"),
        ({"file_paths": "a.c"}, "file_paths"),
        ({"active_file": 3}, "active_file"),
        ({"active_file": "z.c", "file_paths": ["a.c"]}, "one of context.file_paths"),
        ({"kind": "weird"}, "kind"),
    ])
    def test_invalid_context(self, ca, ctx, forbid_bedrock, context, detail):
        status, body = post_code_assist(
            ca, ctx.user, request_body(ctx.usecase_id, context=context))
        assert status == 400
        assert body["error"]["code"] == "INVALID_CONTEXT"
        assert detail in body["error"]["message"]

    def test_plugin_source_is_a_valid_contract(self, ca, admin_ctx, bedrock):
        bedrock.client.converse.return_value = tool_response("project('x', 'c')\n")
        status, body = post_code_assist(ca, admin_ctx.user,
                                        designer_body(admin_ctx.usecase_id))
        assert status == 200, body
        assert body["contract"] == "plugin_source"
        assert body["target_file"] == C_SOURCE


# ------------------------------------------------------- prompts and context

class TestPromptAssembly:
    def test_node_designer_prompt_carries_layout_platforms_and_diagnostics(
            self, ca, admin_ctx, bedrock):
        bedrock.client.converse.return_value = tool_response("#include <gst/gst.h>\n")
        diagnostics = {"kind": "build", "architecture": "arm64_jp6",
                       "text": "gstcustomblurregions.c:12: error: expected ';'"}
        status, body = post_code_assist(
            ca, admin_ctx.user,
            designer_body(admin_ctx.usecase_id, diagnostics=diagnostics))
        assert status == 200, body
        kwargs = bedrock.client.converse.call_args.kwargs
        system = kwargs["system"][0]["text"]
        user_text = kwargs["messages"][0]["content"][0]["text"]

        assert "PLUGIN_SCAFFOLD LAYOUT" in system
        assert HOOK in system and "builds/<arch>/meson.build" in system
        assert "BUILD PLATFORMS:" in system
        assert "arm64_jp7: Ubuntu 24.04 + CUDA 13, GStreamer 1.24" in system
        assert "DIAGNOSTIC MODE" in system and "for architecture arm64_jp6" in system
        assert "`target_file`" in system

        assert "CURRENT FILE CONTENT" in user_text
        assert f"ACTIVE FILE: {C_SOURCE}" in user_text
        assert f"--- {MESON} ---" in user_text and TREE[MESON] in user_text
        assert C_SOURCE not in user_text.split("OTHER FILES:")[1].split("FILE PATHS:")[0]
        assert "FILE PATHS:" in user_text and "- README.md" in user_text
        assert "DIAGNOSTIC OUTPUT (build, arm64_jp6):" in user_text
        assert diagnostics["text"] in user_text
        # The tool schema advertises target_file.
        schema = kwargs["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
        assert "target_file" in schema["properties"]

    def test_omitted_file_contents_are_listed(self, ca, admin_ctx, bedrock):
        bedrock.client.converse.return_value = tool_response("x\n")
        body = designer_body(admin_ctx.usecase_id)
        body["context"]["files"] = {MESON: TREE[MESON]}  # README omitted
        status, _ = post_code_assist(ca, admin_ctx.user, body)
        assert status == 200
        user_text = bedrock.client.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
        assert "- README.md [content omitted]" in user_text
        assert f"- {MESON}\n" in user_text or f"- {MESON}" in user_text

    def test_workflow_builder_accepts_user_diagnostics_and_ignores_files(
            self, ca, ctx, bedrock):
        status, body = post_code_assist(
            ca, ctx.user,
            request_body(ctx.usecase_id, current_code=VALID_CODE,
                         diagnostics={"kind": "user", "text": "NameError: cv3"},
                         context={"files": {"a.c": "x"}, "file_paths": ["a.c"]}))
        assert status == 200, body
        assert "target_file" not in body
        kwargs = bedrock.client.converse.call_args.kwargs
        system = kwargs["system"][0]["text"]
        user_text = kwargs["messages"][0]["content"][0]["text"]
        assert "DIAGNOSTIC MODE" in system and "PLUGIN_SCAFFOLD LAYOUT" not in system
        assert "DIAGNOSTIC OUTPUT (user):" in user_text and "NameError: cv3" in user_text
        assert "OTHER FILES:" not in user_text and "FILE PATHS:" not in user_text

    def test_frame_hook_prompt_unchanged_without_context(self, ca, admin_ctx, bedrock):
        status, body = post_code_assist(
            ca, admin_ctx.user,
            request_body(admin_ctx.usecase_id, surface="node-designer",
                         contract="frame_hook", current_code=TREE[HOOK]))
        assert status == 200, body
        assert body["contract"] == "frame_hook" and "target_file" not in body
        system = bedrock.client.converse.call_args.kwargs["system"][0]["text"]
        assert "TARGET ENTRY POINT: process_frame(frame, params)" in system
        assert "BUILD PLATFORMS:" in system  # node-designer contracts always describe targets


# ------------------------------------------------------------ target files

class TestTargetFile:
    def test_redirect_to_meson_applies_plugin_source_contract(self, ca, admin_ctx, bedrock):
        bedrock.client.converse.return_value = targeted_response(
            "project('blur', 'c')\ndependency('gstreamer-1.0')\n", MESON)
        status, body = post_code_assist(
            ca, admin_ctx.user, designer_body(admin_ctx.usecase_id, active=HOOK,
                                              contract="frame_hook"))
        assert status == 200, body
        assert body["target_file"] == MESON
        assert body["contract"] == "plugin_source"
        assert body["code"].startswith("project(")

    def test_redirect_to_hook_is_entry_point_validated(self, ca, admin_ctx, bedrock):
        bedrock.client.converse.return_value = targeted_response("x = 1\n", HOOK)
        status, body = post_code_assist(
            ca, admin_ctx.user, designer_body(admin_ctx.usecase_id, active=C_SOURCE))
        assert status == 422
        assert body["error"]["code"] == "MISSING_ENTRY_POINT"
        assert body["error"]["details"]["contract"] == "frame_hook"

    def test_hook_target_accepts_valid_hook(self, ca, admin_ctx, bedrock):
        bedrock.client.converse.return_value = targeted_response(TREE[HOOK], HOOK)
        status, body = post_code_assist(
            ca, admin_ctx.user, designer_body(admin_ctx.usecase_id, active=C_SOURCE))
        assert status == 200 and body["contract"] == "frame_hook"
        assert body["target_file"] == HOOK

    def test_out_of_tree_target_is_rejected(self, ca, admin_ctx, bedrock):
        bedrock.client.converse.return_value = targeted_response("x\n", "../evil.c")
        status, body = post_code_assist(
            ca, admin_ctx.user, designer_body(admin_ctx.usecase_id))
        assert status == 422
        assert body["error"]["code"] == "INVALID_TARGET_FILE"
        assert body["error"]["details"]["target_file"] == "../evil.c"

    def test_active_file_defaults_when_no_target(self, ca, admin_ctx, bedrock):
        bedrock.client.converse.return_value = tool_response("#include <gst/gst.h>\n// fixed\n")
        status, body = post_code_assist(
            ca, admin_ctx.user, designer_body(admin_ctx.usecase_id, active=C_SOURCE))
        assert status == 200
        assert body["target_file"] == C_SOURCE and body["contract"] == "plugin_source"

    def test_empty_plugin_source_output_is_no_code(self, ca, admin_ctx, bedrock):
        bedrock.client.converse.return_value = tool_response("   \n")
        status, body = post_code_assist(
            ca, admin_ctx.user, designer_body(admin_ctx.usecase_id))
        assert status == 422
        assert body["error"]["code"] == "NO_CODE_RETURNED"

    def test_node_designer_surface_requires_generate_permission(self, ca, ctx, forbid_bedrock):
        # A DataScientist lacks node-designer:generate.
        status, body = post_code_assist(ca, ctx.user, designer_body(ctx.usecase_id))
        assert status == 403
