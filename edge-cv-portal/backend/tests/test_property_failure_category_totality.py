"""Property test for Bedrock failure category totality
(custom-node-code-assist, task 2.6).

**Feature: custom-node-code-assist, Property 12: Failure category totality**

_For any_ Bedrock error-code string, `code_assist.categorize_bedrock_error`
SHALL return exactly one of `throttling`, `authorization`, `model-access`,
`model-error`, with each code in the design's mapping table landing in its
designated category and every unlisted code falling through to
`model-error`.

**Validates: Requirements 5.1**

`categorize_bedrock_error` is a pure lookup, but importing `code_assist`
pulls in the real `shared_utils` layer, so the module is imported through
the conftest moto stack like the other code-assist backend tests.
"""
import sys

import pytest
from hypothesis import given
from hypothesis import strategies as st

# The four Requirement 5.1 failure categories - the categorization's
# entire codomain.
CATEGORIES = frozenset(
    ('throttling', 'authorization', 'model-access', 'model-error'))

# The design's botocore error code -> category mapping table, verbatim.
DESIGN_MAPPING = {
    'ThrottlingException': 'throttling',
    'TooManyRequestsException': 'throttling',
    'ServiceQuotaExceededException': 'throttling',
    'AccessDeniedException': 'authorization',
    'UnrecognizedClientException': 'authorization',
    'ExpiredTokenException': 'authorization',
    'ResourceNotFoundException': 'model-access',
    'ModelNotReadyException': 'model-access',
    'ValidationException': 'model-access',
    'ModelErrorException': 'model-error',
    'ModelTimeoutException': 'model-error',
    'ServiceUnavailableException': 'model-error',
    'InternalServerException': 'model-error',
}


@pytest.fixture(scope="module")
def code_assist_module(aws_stack):
    """The real code_assist module, imported inside the moto stack so its
    module-level `from shared_utils import ...` binds the real layer."""
    sys.modules.pop("code_assist", None)
    import code_assist
    return code_assist


# Arbitrary error-code strings seeded with the known Bedrock codes so the
# designated rows of the mapping table are always exercised alongside
# unlisted codes.
error_codes = st.one_of(
    st.sampled_from(sorted(DESIGN_MAPPING)),
    st.text(),
)


@given(error_code=error_codes)
def test_failure_category_totality(code_assist_module, error_code):
    """categorize_bedrock_error returns exactly one of the four
    Requirement 5.1 categories for any error-code string, with every code
    in the design's mapping table landing in its designated category and
    every unlisted code categorized as model-error."""
    category = code_assist_module.categorize_bedrock_error(error_code)

    # Totality: the result is always exactly one of the four categories.
    assert category in CATEGORIES, (
        f"categorization of {error_code!r} returned {category!r}, "
        f"outside the four Requirement 5.1 categories")

    # Designated rows: listed codes land in their mapped category;
    # anything else falls through to model-error.
    expected = DESIGN_MAPPING.get(error_code, 'model-error')
    assert category == expected, (
        f"error code {error_code!r} must categorize as {expected!r}, "
        f"got {category!r}")
