"""
Property tests for the code-assist diagnostics / multi-file extension
(custom-node-source-lifecycle tasks 8.3, 8.4).

Property 15: Target-file resolution and contract selection.
Property 16: Diagnostics are bounded and embedded verbatim.

Pure functions only (resolve_target, validate_entry_point,
validate_diagnostics, build_system_prompt, build_user_message); the
module is imported through the test_code_assist fixture so its moto
bindings are in place.
"""
import pytest
from hypothesis import given, strategies as st

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
ARCHES = ["x86_64", "x86_64_nvidia", "arm64_cpu", "arm64_jp5", "arm64_jp6", "arm64_jp7"]

_path = st.from_regex(r"(plugin|builds/[a-z0-9_]{2,8}|docs)/[a-z_]{1,8}\.(c|build|md|h)",
                      fullmatch=True)
_paths = st.lists(_path, min_size=1, max_size=6, unique=True)
_code = st.one_of(
    st.just(""), st.just("   \n"),
    st.just("def process_frame(frame, params):\n    return frame\n"),
    st.just("x = 1\n"),
    st.just("def process_frame(:\n"),
    st.text(min_size=1, max_size=40),
)


@pytest.fixture(scope="module")
def module(ca):
    return ca.code_assist


# --------------------------------------------------------------- Property 15

class TestTargetResolution:
    """**Feature: custom-node-source-lifecycle, Property 15: Target-file
    resolution and contract selection** — Validates: Requirements 5.7, 5.8, 5.9"""

    @given(paths=_paths, include_hook=st.booleans(),
           active_index=st.integers(min_value=0, max_value=6),
           target_choice=st.sampled_from(["absent", "blank", "active", "in_tree", "out_of_tree"]),
           contract=st.sampled_from(["frame_hook", "plugin_source"]))
    def test_resolution_rules(self, module, paths, include_hook, active_index,
                              target_choice, contract):
        file_paths = list(paths) + ([HOOK] if include_hook else [])
        active = file_paths[active_index % len(file_paths)]
        context = {"active_file": active, "file_paths": file_paths}

        if target_choice == "absent":
            target = None
        elif target_choice == "blank":
            target = "   "
        elif target_choice == "active":
            target = active
        elif target_choice == "in_tree":
            target = file_paths[(active_index + 1) % len(file_paths)]
        else:
            target = "../outside/" + active

        resolved, effective, invalid = module.resolve_target(target, contract, context)

        if target_choice == "out_of_tree":
            assert invalid == target.strip() and resolved is None
            assert effective == contract
            return
        assert invalid is None
        expected_target = active if target_choice in ("absent", "blank", "active") else target
        assert resolved == expected_target
        assert effective == ("frame_hook" if resolved == HOOK else "plugin_source")

    @given(contract=st.sampled_from(["process_frame", "process_frame_or_handle", "produce_frame"]),
           target=st.one_of(st.none(), _path))
    def test_workflow_builder_contracts_ignore_targets(self, module, contract, target):
        resolved, effective, invalid = module.resolve_target(
            target, contract, {"active_file": "a.py", "file_paths": ["a.py"]})
        assert (resolved, effective, invalid) == (None, contract, None)

    @given(code=_code)
    def test_plugin_source_accepts_exactly_non_whitespace(self, module, code):
        defect = module.validate_entry_point(code, "plugin_source")
        assert (defect is None) == bool(code.strip())


# --------------------------------------------------------------- Property 16

_diag_text = st.text(min_size=1, max_size=200).filter(lambda t: t.strip())


class TestDiagnosticsEmbedding:
    """**Feature: custom-node-source-lifecycle, Property 16: Diagnostics are
    bounded and embedded verbatim** — Validates: Requirements 5.4, 5.5, 5.10"""

    @given(prompt=st.text(min_size=1, max_size=60).filter(lambda t: t.strip()),
           contract=st.sampled_from(["frame_hook", "plugin_source", "process_frame",
                                     "process_frame_or_handle", "produce_frame"]),
           kind=st.sampled_from(["build", "simulation", "user"]),
           arch=st.one_of(st.none(), st.sampled_from(ARCHES)),
           text=_diag_text,
           current_code=st.one_of(st.none(), st.just(""), st.just("def f():\n    pass\n")))
    def test_messages_embed_diagnostics_verbatim(self, module, prompt, contract, kind,
                                                 arch, text, current_code):
        diagnostics = {"kind": kind, "text": text}
        if arch:
            diagnostics["architecture"] = arch
        assert module.validate_diagnostics(diagnostics) is None

        system = module.build_system_prompt(contract, None, diagnostics)
        user = module.build_user_message(prompt, current_code, None, diagnostics, contract)

        assert prompt in user
        assert text in user
        assert f"DIAGNOSTIC OUTPUT ({kind}" in user
        if arch:
            assert arch in user and f"for architecture {arch}" in system
        assert "DIAGNOSTIC MODE" in system
        if contract in ("frame_hook", "plugin_source"):
            assert "PLUGIN_SCAFFOLD LAYOUT" in system and "BUILD PLATFORMS:" in system
            for known in ARCHES:
                assert f"- {known}:" in system
        else:
            assert "PLUGIN_SCAFFOLD LAYOUT" not in system
        # The current code block is present iff the editor has content.
        has_code = bool(current_code and current_code.strip())
        assert ("CURRENT " in user) == has_code

    @given(length=st.integers(min_value=16 * 1024 + 1, max_value=16 * 1024 + 500))
    def test_oversized_text_rejected(self, module, length):
        err = module.validate_diagnostics({"kind": "user", "text": "e" * length})
        assert err is not None and err["statusCode"] == 400

    @given(length=st.integers(min_value=1, max_value=16 * 1024))
    def test_text_within_bound_accepted(self, module, length):
        assert module.validate_diagnostics({"kind": "user", "text": "e" * length}) is None
