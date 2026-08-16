"""Property test for prompt template persistence (synthetic-defect-data-
generation, task 4.4).

**Feature: synthetic-defect-data-generation, Property 1: Prompt template
lookup and persistence round trip**

_For any_ Use_Case, Object_Type, and Defect_Type: saving a Prompt_Template
and then loading it for that same key returns exactly the saved text,
saving under one key never alters the template stored under a different
key, and loading a key with no stored template returns the default
template containing both the ``{object_type}`` and ``{defect_type}``
placeholder variables.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

Runs the real GET/PUT /synthetic/prompt-templates handlers against moto
DynamoDB (conftest.py + synthetic_env.py).
"""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from synthetic_env import SyntheticEnv

# Object_Type / Defect_Type values: printable, non-empty, without '#'
# (the composite template_key separator) so distinct pairs always map to
# distinct keys.
type_names = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=0x2FF,
                           exclude_characters="#"),
    min_size=1, max_size=24,
).filter(lambda s: s.strip())

template_texts = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=0x2FF),
    min_size=1, max_size=200,
)

type_pairs = st.tuples(type_names, type_names)


@pytest.fixture(scope="module")
def senv(aws_stack):
    return SyntheticEnv(aws_stack)


def _get_template(senv, user, usecase_id, object_type, defect_type):
    return senv.invoke(
        "GET", "/synthetic/prompt-templates", user,
        query={"usecase_id": usecase_id, "object_type": object_type,
               "defect_type": defect_type})


def _put_template(senv, user, usecase_id, object_type, defect_type, text):
    return senv.invoke(
        "PUT", "/synthetic/prompt-templates", user,
        body={"usecase_id": usecase_id, "object_type": object_type,
              "defect_type": defect_type, "template_text": text})


@settings(deadline=None)
@given(
    pairs=st.lists(type_pairs, min_size=2, max_size=4, unique=True),
    texts=st.lists(template_texts, min_size=4, max_size=4),
)
def test_prompt_template_round_trip_and_isolation(senv, pairs, texts):
    """Save-then-load returns the saved text (2.1, 2.2, 2.4); saving one
    key never alters another (2.1); a missing key returns the default
    containing both placeholders (2.3)."""
    usecase_id = senv.create_usecase()
    user = senv.actor_with_role(usecase_id, "DataScientist")

    saved_pair, other_pair = pairs[0], pairs[1]
    saved_text, other_text = texts[0], texts[1]

    # Missing key -> default template with both placeholder variables
    # (Req 2.3).
    status, body = _get_template(senv, user, usecase_id, *saved_pair)
    assert status == 200
    assert body["is_default"] is True
    assert "{object_type}" in body["template_text"]
    assert "{defect_type}" in body["template_text"]

    # Save both keys, then round-trip each (Req 2.1, 2.2, 2.4).
    status, _ = _put_template(senv, user, usecase_id, *saved_pair,
                              saved_text)
    assert status == 200
    status, _ = _put_template(senv, user, usecase_id, *other_pair,
                              other_text)
    assert status == 200

    status, body = _get_template(senv, user, usecase_id, *saved_pair)
    assert status == 200
    assert body["is_default"] is False
    assert body["template_text"] == saved_text

    # Overwriting one key never alters the other key's stored text.
    updated_text = texts[2]
    status, _ = _put_template(senv, user, usecase_id, *saved_pair,
                              updated_text)
    assert status == 200

    status, body = _get_template(senv, user, usecase_id, *saved_pair)
    assert body["template_text"] == updated_text
    status, body = _get_template(senv, user, usecase_id, *other_pair)
    assert body["template_text"] == other_text, (
        "saving one key must not alter the template stored under a "
        "different key")

    # Templates are keyed by Use_Case too (Req 2.1): another Use_Case
    # still sees the default for the same type pair.
    other_usecase = senv.create_usecase("Other Use Case")
    other_user = senv.actor_with_role(other_usecase, "DataScientist")
    status, body = _get_template(senv, other_user, other_usecase,
                                 *saved_pair)
    assert status == 200
    assert body["is_default"] is True
