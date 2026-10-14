"""Property test for parser rejection of invalid documents (task 2.5).

**Feature: workflow-manager, Property 2: Parser rejects invalid documents descriptively**

For all invalid JSON documents (random junk, or valid documents corrupted
by random schema-violating mutations), ``parse`` returns a descriptive
error identifying a violation location, never a graph and never an
unhandled exception.

**Validates: Requirements 3.3**
"""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from workflow_core.serializer import ParseError, ParseResult, parse

from .generators import corrupted_document_strategy


# ---------------------------------------------------------------------------
# Invalid-document generators
#
# Three families, each invalid *by construction* so the property never
# quantifies over an accidentally valid document:
#
# 1. Random junk that is not valid JSON at all.
# 2. Valid JSON whose top-level value is not an object (the schema
#    requires an object), covering scalars, arrays, and nested values.
# 3. Well-formed documents corrupted by a random schema-violating
#    mutation (shared ``corrupted_document_strategy``: dropped required
#    keys, wrong types, bad schema versions, extra properties, duplicate
#    ids, dangling node references).
# ---------------------------------------------------------------------------

def _is_not_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return True
    return False


#: Random junk strings that fail JSON decoding outright.
non_json_junk = st.text(max_size=50).filter(_is_not_json)

#: Valid JSON documents whose top-level value is not an object.
_non_object_json_values = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
    st.lists(
        st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=10)),
        max_size=5,
    ),
)
non_object_documents = _non_object_json_values.map(json.dumps)

invalid_documents = st.one_of(
    non_json_junk,
    non_object_documents,
    corrupted_document_strategy(),
)


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------

@given(document=invalid_documents)
def test_parser_rejects_invalid_documents_descriptively(document):
    """**Feature: workflow-manager, Property 2: Parser rejects invalid documents descriptively**

    **Validates: Requirements 3.3**
    """
    # Never an unhandled exception: any raise here fails the property.
    result = parse(document)

    # Never a graph.
    assert isinstance(result, ParseResult)
    assert not result.ok, "invalid document was parsed successfully"
    assert result.graph is None, "invalid document produced a graph"

    # A descriptive error identifying a violation location.
    error = result.error
    assert isinstance(error, ParseError)
    assert isinstance(error.code, str) and error.code, "error missing a code"
    assert isinstance(error.message, str) and error.message.strip(), (
        "error missing a descriptive message"
    )
    # The location is a JSON pointer (RFC 6901): the empty string denotes
    # the document root; any other pointer must start with "/".
    assert isinstance(error.path, str), "error missing a violation path"
    assert error.path == "" or error.path.startswith("/"), (
        "violation path %r is not a JSON pointer" % error.path
    )
