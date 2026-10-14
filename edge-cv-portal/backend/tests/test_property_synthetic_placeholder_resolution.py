"""Property test for prompt placeholder resolution (synthetic-defect-data-
generation, task 2.2).

**Feature: synthetic-defect-data-generation, Property 2: Placeholder
resolution totality**

_For any_ Prompt_Template text and resolution context: if every placeholder
in the template is present in the context, resolution succeeds, the result
contains no remaining placeholder tokens, and every context value referenced
appears substituted in the result; if any placeholder is missing from the
context, resolution rejects the request and reports exactly the set of
unresolved placeholder names.

**Validates: Requirements 2.5, 2.6**

Pure-logic test over synthetic_core.resolve_prompt: no AWS mocks. Templates
are built constructively from literal segments (with ``{``/``}`` escaped as
``{{``/``}}``) and ``{name}`` placeholders, so the expected resolved text is
known exactly.
"""
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from synthetic_core import (
    DEFAULT_PROMPT_TEMPLATE,
    UnresolvedPlaceholderError,
    extract_placeholders,
    resolve_prompt,
)

_PLACEHOLDER_TOKEN_RE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Placeholder identifiers per the grammar [A-Za-z_][A-Za-z0-9_]*.
identifiers = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,15}", fullmatch=True)

# Literal template text: arbitrary printable text (braces included - they
# get escaped when the template is assembled).
literal_text = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=0x2FF),
    max_size=30,
)

# Context values: printable text without braces, so the resolved output can
# be checked for remaining placeholder tokens without false positives.
context_values = st.text(
    alphabet=st.characters(
        min_codepoint=32, max_codepoint=0x2FF, exclude_characters="{}"),
    max_size=30,
)


def _escape_literal(text):
    return text.replace("{", "{{").replace("}", "}}")


@st.composite
def template_cases(draw, min_placeholders=0):
    """(template, expected_resolved_fn, used_names, context) where the
    template interleaves escaped literal segments and placeholders."""
    name_pool = draw(st.lists(identifiers, min_size=1, max_size=5,
                              unique=True))
    segments = draw(st.lists(
        st.one_of(
            st.tuples(st.just("lit"), literal_text),
            st.tuples(st.just("ph"), st.sampled_from(name_pool)),
        ),
        min_size=min_placeholders, max_size=10,
    ))
    if min_placeholders and not any(k == "ph" for k, _ in segments):
        forced = draw(st.sampled_from(name_pool))
        segments = segments + [("ph", forced)]

    used_names = []
    for kind, value in segments:
        if kind == "ph" and value not in used_names:
            used_names.append(value)

    context = {name: draw(context_values) for name in used_names}

    template = "".join(
        _escape_literal(value) if kind == "lit" else "{" + value + "}"
        for kind, value in segments
    )
    expected = "".join(
        value if kind == "lit" else context[value]
        for kind, value in segments
    )
    return template, expected, used_names, context


@settings(deadline=None)
@given(case=template_cases())
def test_full_context_resolution_succeeds_totally(case):
    """When every placeholder is present in the context, resolution
    succeeds, matches the constructively expected text, leaves no
    placeholder tokens, and substitutes every referenced value
    (Requirement 2.5)."""
    template, expected, used_names, context = case

    result = resolve_prompt(template, context)

    assert result == expected

    # No remaining placeholder tokens in the result.
    remaining = [m.group(1) for m in _PLACEHOLDER_TOKEN_RE.finditer(result)
                 if m.group(1) is not None]
    assert remaining == [], (
        f"resolved prompt still contains placeholder tokens: {remaining!r}")

    # Every referenced context value appears substituted in the result.
    for name in used_names:
        assert context[name] in result


@settings(deadline=None)
@given(case=template_cases(min_placeholders=1), data=st.data())
def test_missing_placeholders_reject_with_exact_names(case, data):
    """When any placeholder is missing from the context, resolution rejects
    the request and reports exactly the set of unresolved placeholder names
    (Requirement 2.6)."""
    template, _, used_names, context = case
    missing = data.draw(st.lists(st.sampled_from(used_names), min_size=1,
                                 max_size=len(used_names), unique=True))
    partial_context = {k: v for k, v in context.items() if k not in missing}

    with pytest.raises(UnresolvedPlaceholderError) as exc_info:
        resolve_prompt(template, partial_context)

    assert set(exc_info.value.names) == set(missing)
    # Every unresolved name is surfaced in the user-facing message.
    for name in missing:
        assert name in str(exc_info.value)


@settings(deadline=None)
@given(extra=st.dictionaries(identifiers, context_values, max_size=3))
def test_default_template_resolves_with_object_and_defect_type(extra):
    """The default template's placeholders are exactly object_type and
    defect_type, and it resolves with any context providing both
    (Requirements 2.5 supporting 2.3)."""
    assert set(extract_placeholders(DEFAULT_PROMPT_TEMPLATE)) == {
        "object_type", "defect_type"}

    context = dict(extra)
    context["object_type"] = "metal casting"
    context["defect_type"] = "scratch"
    result = resolve_prompt(DEFAULT_PROMPT_TEMPLATE, context)
    assert "metal casting" in result
    assert "scratch" in result
