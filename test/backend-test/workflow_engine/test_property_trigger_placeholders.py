# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Property test for dotted trigger placeholder resolution (Task 6.4).

**Feature: custom-python-source, Property 3: Dotted trigger placeholders
resolve from the seeded metadata**

*For any* Trigger_Context containing nested dict values and any dotted
path addressing a value inside it, rendering a prompt template containing
``{trigger.<path>}`` against Run_Metadata seeded with that context
substitutes ``str(value)`` at the placeholder — exercising the unchanged
``llm_inference.render_prompt`` dotted-placeholder engine against exactly
the ``tag_values["trigger"] = context`` shape the executor seeds.

**Validates: Requirements 2.6**
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.llm_inference import render_prompt

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Path segments valid under the placeholder grammar
#: ``[A-Za-z_][A-Za-z0-9_.]*`` (segments after the first dot may start
#: with any word character).
_KEYS = st.from_regex(r"[a-z_][a-z0-9_]{0,9}", fullmatch=True)

#: Leaf values whose str() lands in the rendered prompt.
_LEAF_VALUES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10 ** 12), max_value=10 ** 12),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=30),
)

#: Literal template text around the placeholder: no braces (brace escaping
#: is render_prompt's own concern, not this property's).
_LITERAL_TEXT = st.text(max_size=20).filter(
    lambda s: "{" not in s and "}" not in s
)


@st.composite
def _seeded_contexts(draw):
    """A Trigger_Context with nested dict values, a dotted path into it,
    and the value at that path. Sibling entries are added at every level
    so resolution must actually navigate the path."""
    path = draw(st.lists(_KEYS, min_size=1, max_size=4, unique=True))
    leaf = draw(_LEAF_VALUES)

    value = leaf
    for key in reversed(path):
        node = {key: value}
        # Sibling entries beside the path at this level.
        siblings = draw(st.dictionaries(
            _KEYS.filter(lambda k, taken=key: k != taken),
            _LEAF_VALUES,
            max_size=2,
        ))
        node.update(siblings)
        value = node
    return value, path, leaf


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: custom-python-source, Property 3: Dotted trigger placeholders
# resolve from the seeded metadata
@settings(max_examples=100, deadline=None)
@given(
    context_path_leaf=_seeded_contexts(),
    prefix=_LITERAL_TEXT,
    suffix=_LITERAL_TEXT,
)
def test_dotted_trigger_placeholders_resolve(context_path_leaf, prefix,
                                             suffix):
    """For any Trigger_Context with nested dict values and any dotted
    path into it, a prompt template containing ``{trigger.<path>}``
    rendered against the seeded Run_Metadata substitutes ``str(value)``
    at the placeholder, preserving the surrounding literal text.

    **Validates: Requirements 2.6**
    """
    context, path, leaf = context_path_leaf

    # The exact Run_Metadata shape the executor seeds (tag_values with
    # the run's Trigger_Context under `trigger`).
    metadata = {"trigger": context}

    template = "{0}{{trigger.{1}}}{2}".format(prefix, ".".join(path), suffix)

    rendered = render_prompt(template, metadata)

    assert rendered == prefix + str(leaf) + suffix
