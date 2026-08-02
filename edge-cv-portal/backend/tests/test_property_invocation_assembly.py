"""Property test for invocation assembly
(custom-node-code-assist, task 2.4).

**Feature: custom-node-code-assist, Property 2: Invocation assembly**

_For any_ prompt, contract, and editor content (including unicode and
whitespace-only strings): the assembled Converse messages contain the
prompt verbatim; the system prompt carries the contract's entry-point
signature and its environment description markers (`dda_frames` and the
pre-bound `cv2`/`np` bindings for the Python_Bridge contracts;
`params` for `frame_hook`); and the user message embeds the editor
content in the modify-this-module block if and only if the editor
content contains at least one non-whitespace character.

**Validates: Requirements 2.1, 2.6, 2.10**

`build_system_prompt` and `build_user_message` are pure functions over
already-validated inputs, so this test needs no moto stack - it drives
the functions directly.
"""
import sys

import pytest
from hypothesis import given
from hypothesis import strategies as st

CONTRACT_NAMES = ("process_frame", "process_frame_or_handle", "frame_hook")

# The Workflow_Builder contracts executed by the Python_Bridge runner
# (frame_hook runs in the GStreamer element's embedded interpreter).
PYTHON_BRIDGE_CONTRACTS = frozenset({"process_frame", "process_frame_or_handle"})


@pytest.fixture(scope="module")
def code_assist():
    """Import the real code_assist module against the real shared_utils
    layer.

    code_assist imports shared_utils at module scope, and standalone
    tests in this directory (e.g. test_captures.py) install a *fake*
    shared_utils into sys.modules at collection time that lacks the
    names code_assist needs (rbac_manager, Permission, log_audit_event).
    conftest.py already put the real layer and functions directories on
    sys.path, so popping both modules and re-importing binds the real
    ones regardless of collection order.
    """
    sys.modules.pop("shared_utils", None)
    sys.modules.pop("code_assist", None)
    import code_assist as module
    return module


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Valid prompts (Requirement 1.4's constraint: at least one non-whitespace
# character): arbitrary unicode text, capped well below 4,000 for speed.
prompts = st.text(min_size=1, max_size=300).filter(lambda s: s.strip())

# Editor content: absent, empty, whitespace-only (spaces/tabs/newlines and
# unicode whitespace), arbitrary unicode text, or realistic module code.
whitespace_only = st.text(
    alphabet=" \t\n\r\x0b\f\u00a0\u2003", max_size=20)

code_snippets = st.sampled_from([
    "def process_frame(frame, metadata):\n    return None\n",
    "import cv2\n\ndef handle(frame_bytes, metadata):\n"
    "    return frame_bytes, metadata\n",
    "# just a comment\nx = 1\n",
    "def process_frame(frame, params):\n    return frame\n",
])

editor_contents = st.one_of(
    st.none(),
    whitespace_only,             # includes the empty string
    st.text(max_size=300),       # arbitrary unicode, may be whitespace-only
    code_snippets,
)

# Optional frame_hook context: absent, empty, or carrying a declared
# element parameter list (well-formed and malformed entries alike - the
# assembly must tolerate both without affecting the markers).
parameter_entries = st.fixed_dictionaries(
    {"name": st.text(alphabet="abcdefghijklmnopqrstuvwxyz-_",
                     min_size=1, max_size=12),
     "param_type": st.sampled_from(["int", "float", "string", "boolean"])},
    optional={"description": st.text(max_size=30)},
)

contexts = st.one_of(
    st.none(),
    st.just({}),
    st.fixed_dictionaries(
        {}, optional={"parameters": st.lists(parameter_entries, max_size=3)}),
)


# ---------------------------------------------------------------------------
# Property 2: Invocation assembly
# ---------------------------------------------------------------------------

@given(prompt=prompts,
       contract=st.sampled_from(CONTRACT_NAMES),
       current_code=editor_contents,
       context=contexts)
def test_invocation_assembly(code_assist, prompt, contract,
                             current_code, context):
    """For any prompt, contract, and editor content: the prompt appears
    verbatim in the user message; the system prompt carries the
    contract's entry-point signature and environment markers; and the
    editor content is embedded in the modify-this-module block iff it
    contains a non-whitespace character (Requirements 2.1, 2.6, 2.10)."""
    spec = code_assist.CONTRACTS[contract]

    system_prompt = code_assist.build_system_prompt(contract, context)
    user_message = code_assist.build_user_message(prompt, current_code)

    # The prompt appears verbatim in the user message (Requirement 2.1).
    assert prompt in user_message, (
        f"prompt must appear verbatim in the user message, "
        f"got {user_message!r}")

    # The system prompt carries the contract's entry-point signature
    # (Requirement 2.1).
    assert spec["signature"] in system_prompt, (
        f"system prompt for {contract} must carry the entry-point "
        f"signature {spec['signature']!r}")

    # Environment markers per contract (Requirement 2.1): the
    # Python_Bridge contracts describe the dda_frames helper and the
    # pre-bound cv2/np bindings; frame_hook describes the `params` dict.
    if contract in PYTHON_BRIDGE_CONTRACTS:
        for marker in ("dda_frames", "cv2", "np", "pre-bound"):
            assert marker in system_prompt, (
                f"system prompt for {contract} must carry the "
                f"Python_Bridge environment marker {marker!r}")
    else:
        assert "params" in system_prompt, (
            "system prompt for frame_hook must carry the `params` "
            "environment marker")

    # The editor content is embedded in the modify-this-module block iff
    # it contains a non-whitespace character (Requirements 2.6, 2.10).
    should_embed = bool(current_code) and bool(current_code.strip())
    if should_embed:
        assert "CURRENT MODULE CODE:" in user_message, (
            "non-whitespace editor content must be presented in the "
            "modify-this-module block")
        assert current_code in user_message, (
            "the editor content must appear verbatim inside the "
            "modify-this-module block")
    else:
        # Empty/whitespace-only editors are treated as empty: the prompt
        # is sent alone and nothing is presented as code to modify.
        assert user_message == prompt, (
            f"empty/whitespace-only editor content must send the prompt "
            f"alone, got {user_message!r}")
