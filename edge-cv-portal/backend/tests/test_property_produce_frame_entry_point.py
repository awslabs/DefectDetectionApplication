"""Property test for produce_frame entry-point validation
(custom-python-source, task 4.2).

**Feature: custom-python-source, Property 21: Entry-point validation
accepts exactly modules defining produce_frame**

_For any_ syntactically valid Python module,
`validate_entry_point(code, "produce_frame")` passes exactly when the
module defines a top-level function named `produce_frame` - nested
definitions (inside functions, conditional blocks, or classes), other
names, and assignments named `produce_frame` do not count - and otherwise
reports the missing-entry-point defect.

**Validates: Requirements 9.5**

`validate_entry_point` is pure (ast.parse + top-level FunctionDef
inspection), but importing `code_assist` pulls in the real `shared_utils`
layer, so the module is imported through the conftest moto stack like the
other code-assist backend tests (test_property_entry_point_validation
pattern).
"""
import sys

import pytest
from hypothesis import given
from hypothesis import strategies as st

ENTRY_NAME = 'produce_frame'

# Top-level decoy definitions that must never satisfy the contract,
# including the other contracts' entry points.
DECOY_NAMES = ('process_frame', 'handle', 'produce_frames', 'produce',
               'main', 'helper', 'frame_producer', 'run')

# Top-level assignments binding the entry-point name without a
# FunctionDef - none of these may count (Property 21: "assignments do
# not count").
ASSIGNMENT_FORMS = (
    'produce_frame = None',
    'produce_frame = lambda context: None',
    'produce_frame = helper_value',
)


@pytest.fixture(scope="module")
def code_assist_module(aws_stack):
    """The real code_assist module, imported inside the moto stack so its
    module-level `from shared_utils import ...` binds the real layer."""
    sys.modules.pop("code_assist", None)
    import code_assist
    return code_assist


def _function_lines(name, indent, params='context'):
    pad = ' ' * indent
    return [f'{pad}def {name}({params}):',
            f'{pad}    return None']


@st.composite
def synthesized_modules(draw):
    """(code, has_top_level_entry): a syntactically valid module planting
    a controlled mix of top-level definitions (the entry point and
    decoys), `produce_frame` definitions nested inside a function body, a
    conditional block, and a class body, and top-level assignments named
    `produce_frame` - only a top-level `def produce_frame` may count."""
    top_level_entry = draw(st.booleans())
    top_decoys = draw(st.lists(
        st.sampled_from(DECOY_NAMES), unique=True, min_size=0, max_size=4))
    nested_in_function = draw(st.booleans())
    nested_in_conditional = draw(st.booleans())
    nested_in_class = draw(st.booleans())
    assignments = draw(st.lists(
        st.sampled_from(ASSIGNMENT_FORMS), unique=True,
        min_size=0, max_size=2))

    lines = ['import sys', '', 'helper_value = 3', '']

    for form in assignments:
        lines.append(form)
        lines.append('')

    for name in top_decoys:
        lines += _function_lines(name, 0, params='frame, metadata')
        lines.append('')

    if top_level_entry:
        lines += _function_lines(ENTRY_NAME, 0)
        lines.append('')

    if nested_in_function:
        lines.append('def _outer_scope():')
        lines += _function_lines(ENTRY_NAME, 4)
        lines.append('    return None')
        lines.append('')

    if nested_in_conditional:
        lines.append('if helper_value > 1:')
        lines += _function_lines(ENTRY_NAME, 4)
        lines.append('')

    if nested_in_class:
        lines.append('class Producer:')
        lines += _function_lines(ENTRY_NAME, 4, params='self, context')
        lines.append('')

    return '\n'.join(lines), top_level_entry


@given(case=synthesized_modules())
def test_produce_frame_entry_point_validation(code_assist_module, case):
    """validate_entry_point(code, 'produce_frame') passes exactly when a
    top-level function named produce_frame is defined; nested
    definitions, other names, and assignments do not count, and every
    rejection is the missing-entry-point defect (Requirement 9.5)."""
    code, has_top_level_entry = case

    defect = code_assist_module.validate_entry_point(code, 'produce_frame')

    if has_top_level_entry:
        assert defect is None, (
            f"a module with a top-level def produce_frame must pass, "
            f"got {defect!r}\n---\n{code}")
    else:
        assert defect is not None, (
            f"a module without a top-level def produce_frame must be "
            f"rejected\n---\n{code}")
        assert defect.startswith('missing entry point'), (
            f"the rejection must be the missing-entry-point defect, "
            f"got {defect!r}")
        assert 'produce_frame(context)' in defect, (
            f"the defect must name the contract signature, got {defect!r}")
