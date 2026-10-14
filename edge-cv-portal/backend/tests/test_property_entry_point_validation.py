"""Property test for entry-point validation
(custom-node-code-assist, task 2.5).

**Feature: custom-node-code-assist, Property 3: Entry-point validation**

_For any_ synthesized Python module with controlled top-level and nested
function definitions (and for invalid sources), and any Node_Contract,
`code_assist.validate_entry_point(code, contract)` SHALL return no defect
if and only if the source parses and the module's top-level function
definitions satisfy the contract's rule - at least one entry-point match
for `process_frame`/`frame_hook`, exactly one of {`process_frame`,
`handle`} for `process_frame_or_handle`. Nested definitions (inside
functions, conditional blocks, or classes) never satisfy the contract,
and an unparseable source always yields the invalid-Python defect.

**Validates: Requirements 2.2, 2.3, 5.6**

`validate_entry_point` is pure (ast.parse + top-level FunctionDef
inspection), but importing `code_assist` pulls in the real `shared_utils`
layer, so the module is imported through the conftest moto stack like the
other code-assist backend tests.
"""
import sys

import pytest
from hypothesis import given
from hypothesis import strategies as st

# The two entry-point names any contract can require.
ENTRY_NAMES = ('process_frame', 'handle')

# Top-level decoys that must never satisfy a contract.
DECOY_NAMES = ('main', 'helper', 'process_frames', 'handler', 'run',
               'frame_hook', 'process')

# Corruption suffixes that make any module unparseable regardless of what
# precedes them (each is a guaranteed SyntaxError at column 0).
CORRUPTIONS = (
    'def broken(:',        # malformed def
    'x = (',               # unterminated parenthesis at EOF
    'for in range(3):',    # missing loop target
)


@pytest.fixture(scope="module")
def code_assist_module(aws_stack):
    """The real code_assist module, imported inside the moto stack so its
    module-level `from shared_utils import ...` binds the real layer."""
    sys.modules.pop("code_assist", None)
    import code_assist
    return code_assist


def _function_lines(name, indent, params='frame, metadata'):
    pad = ' ' * indent
    return [f'{pad}def {name}({params}):',
            f'{pad}    return None']


@st.composite
def synthesized_modules(draw):
    """(code, parses, top_level_names): a module planting a controlled set
    of top-level definitions (entry points and decoys) among filler
    statements, plus entry-point-named definitions nested inside a
    function body, a conditional block, and a class body - none of which
    may count as top-level. Optionally corrupted into invalid Python."""
    top_names = draw(st.lists(
        st.sampled_from(ENTRY_NAMES + DECOY_NAMES),
        unique=True, min_size=0, max_size=5))
    nested_in_function = draw(st.lists(
        st.sampled_from(ENTRY_NAMES), unique=True, max_size=2))
    nested_in_conditional = draw(st.lists(
        st.sampled_from(ENTRY_NAMES), unique=True, max_size=2))
    nested_in_class = draw(st.lists(
        st.sampled_from(ENTRY_NAMES), unique=True, max_size=2))

    lines = ['import sys', '', 'THRESHOLD = 3', '']

    for name in top_names:
        lines += _function_lines(name, 0)
        lines.append('')

    if nested_in_function:
        lines.append('def _outer_scope():')
        for name in nested_in_function:
            lines += _function_lines(name, 4)
        lines.append('    return None')
        lines.append('')

    if nested_in_conditional:
        lines.append('if THRESHOLD > 1:')
        for name in nested_in_conditional:
            lines += _function_lines(name, 4)
        lines.append('')

    if nested_in_class:
        lines.append('class Processor:')
        for name in nested_in_class:
            lines += _function_lines(name, 4, params='self, frame, metadata')
        lines.append('')

    corruption = draw(st.one_of(st.none(), st.sampled_from(CORRUPTIONS)))
    if corruption is not None:
        lines.append(corruption)

    return '\n'.join(lines), corruption is None, frozenset(top_names)


@given(case=synthesized_modules(),
       contract=st.sampled_from(('process_frame',
                                 'process_frame_or_handle',
                                 'frame_hook')))
def test_entry_point_validation(code_assist_module, case, contract):
    """validate_entry_point returns no defect iff the source parses and
    the top-level definitions satisfy the contract's rule: at least one
    entry-point match for process_frame/frame_hook, exactly one of
    {process_frame, handle} for process_frame_or_handle (Requirements
    2.2, 2.3, 5.6)."""
    code, parses, top_names = case
    spec = code_assist_module.CONTRACTS[contract]

    defect = code_assist_module.validate_entry_point(code, contract)

    if not parses:
        # An unparseable source is always the invalid-Python defect,
        # regardless of what names appear in the text (5.6 / 2.2).
        assert defect is not None and defect.startswith(
            code_assist_module.INVALID_PYTHON_PREFIX), (
            f"invalid source must yield the invalid-Python defect, "
            f"got {defect!r}")
        return

    defined = top_names & spec['entry_points']
    if spec['require_exactly_one']:
        expected_valid = len(defined) == 1          # Requirement 2.3
    else:
        expected_valid = len(defined) >= 1          # Requirements 2.2, 5.6

    assert (defect is None) == expected_valid, (
        f"contract {contract!r} with top-level entry points "
        f"{sorted(defined)} (of top-level defs {sorted(top_names)}) must "
        f"{'be valid' if expected_valid else 'be a defect'}, got {defect!r}")

    if defect is not None:
        # The defect kind matches the violated rule, and a parse-ok module
        # never yields the invalid-Python defect.
        assert not defect.startswith(
            code_assist_module.INVALID_PYTHON_PREFIX)
        if not defined:
            assert defect.startswith('missing entry point'), (
                f"zero matches must be the missing-entry-point defect, "
                f"got {defect!r}")
        else:
            assert 'both entry points' in defect, (
                f"both process_frame and handle defined under "
                f"require_exactly_one must be the both-defined defect, "
                f"got {defect!r}")
