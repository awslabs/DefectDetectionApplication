"""Property test for Plugin_Scaffold completeness (task 1.7).

**Feature: custom-node-designer, Property 2: Scaffold generation is complete for the declaration**

For all valid Custom_Node_Type declarations, the generated Plugin_Scaffold
contains the Frame_Processing_Hook source file, exactly one build
configuration per selected Target_Architecture, and parameter plumbing
that exposes every declared parameter name to the hook.

**Validates: Requirements 1.2, 1.4**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import DEEPSTREAM_ARCHITECTURES, DEVICE_ARCHITECTURES
from workflow_core.scaffold import (
    HOOK_FILE,
    build_config_path,
    c_source_path,
    render_scaffold,
)

from .test_property_declaration_conversion import valid_declarations

# ---------------------------------------------------------------------------
# Strategy: valid declarations with a Target_Architecture selection
# ---------------------------------------------------------------------------


@st.composite
def declarations_with_architectures(draw):
    """(declaration, expected selected Target_Architectures).

    Reuses the Property 1 valid-declaration strategy (node-catalog wire
    shape) and either adds an explicit ``architectures`` selection or
    omits the key to exercise the fallback to the declaration's mapping
    architectures. DeepStream-flagged declarations select only JetPack
    architectures, matching the declaration-level restriction.
    """
    declaration = draw(valid_declarations())

    # The selection the omitted-key fallback would produce: the
    # declaration's mapping architectures restricted to device
    # architectures (mappings may also name non-device architectures
    # such as the simulation architecture, which carry no build).
    fallback = [m["arch"] for m in declaration["mappings"]
                if m["arch"] in DEVICE_ARCHITECTURES]

    if not fallback or draw(st.booleans()):
        pool = (DEEPSTREAM_ARCHITECTURES if declaration["deepstream"]
                else DEVICE_ARCHITECTURES)
        selected = draw(st.lists(
            st.sampled_from(pool), min_size=1, max_size=len(pool),
            unique=True))
        declaration["architectures"] = list(selected)
    else:
        selected = fallback

    return declaration, selected


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------


@settings(max_examples=25)
@given(case=declarations_with_architectures())
def test_scaffold_generation_is_complete_for_the_declaration(case):
    """**Feature: custom-node-designer, Property 2: Scaffold generation is complete for the declaration**

    **Validates: Requirements 1.2, 1.4**
    """
    declaration, selected = case
    files = render_scaffold(declaration)

    # The scaffold is a file map of non-empty sources (Requirement 1.2).
    assert isinstance(files, dict)
    for path, content in files.items():
        assert isinstance(path, str) and path
        assert isinstance(content, str) and content.strip(), path

    # The Frame_Processing_Hook source file is present and carries the
    # hook contract the user fills in (Requirement 1.2).
    assert HOOK_FILE in files
    hook = files[HOOK_FILE]
    assert "def process_frame(frame, params):" in hook

    # Exactly one build configuration per selected Target_Architecture:
    # every selected architecture has its configuration, and no build
    # configuration exists for an unselected architecture (Requirement 1.2).
    expected_build_configs = {build_config_path(arch) for arch in selected}
    actual_build_configs = {
        path for path in files
        if path in {build_config_path(a) for a in DEVICE_ARCHITECTURES}
    }
    assert actual_build_configs == expected_build_configs
    assert len(expected_build_configs) == len(selected)

    # Every declared parameter name is exposed to the hook: listed in the
    # hook file's declared-parameters map and plumbed into the params
    # dict built by the C skeleton element (Requirement 1.4).
    c_source = files[c_source_path(declaration)]
    for parameter in declaration["parameters"]:
        name = parameter["name"]
        assert repr(name) in hook
        assert 'PyDict_SetItemString (params, "{0}"'.format(name) in c_source
