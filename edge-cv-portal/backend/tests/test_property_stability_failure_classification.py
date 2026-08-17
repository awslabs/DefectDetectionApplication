"""Property test for Bedrock invocation failure classification
(stability-generation-models, task 3.7).

**Feature: stability-generation-models, Property 10: Bedrock invocation
failure classification is total**

_For any_ error code, error message, and model id:
``classify_bedrock_invocation_error`` always returns a non-empty reason;
AccessDeniedException maps to a reason identifying that Bedrock model
access is not granted and containing the model id; ResourceNotFoundException
with a message marking the model as Legacy maps to a reason identifying
the model's lifecycle status; every other code maps to a passthrough
reason containing the original code and message.

**Validates: Requirements 9.1, 9.2**

Pure-logic test over synthetic_core.classify_bedrock_invocation_error:
no AWS mocks.
"""
from hypothesis import example, given, settings
from hypothesis import strategies as st

from synthetic_core import classify_bedrock_invocation_error


error_codes = st.one_of(
    st.sampled_from([
        "AccessDeniedException",
        "ResourceNotFoundException",
        "ThrottlingException",
        "ValidationException",
        "ServiceUnavailableException",
        "ModelTimeoutException",
        "",
    ]),
    st.text(min_size=1, max_size=40),
)

error_messages = st.text(max_size=200)

model_ids = st.sampled_from([
    "amazon.nova-canvas-v1:0",
    "us.stability.stable-image-inpaint-v1:0",
    "stability.stable-image-inpaint-v1:0",
])

_LEGACY_MESSAGE = (
    "The provided model identifier is invalid: amazon.nova-canvas-v1:0 "
    "is marked by provider as Legacy and you have not been actively "
    "using the model in the last 30 days.")


@settings(deadline=None)
@example(error_code="AccessDeniedException",
         error_message="You don't have access to the model.",
         model_id="us.stability.stable-image-inpaint-v1:0")
@example(error_code="ResourceNotFoundException",
         error_message=_LEGACY_MESSAGE,
         model_id="amazon.nova-canvas-v1:0")
@example(error_code="ResourceNotFoundException",
         error_message="Model not found.",
         model_id="amazon.nova-canvas-v1:0")
@example(error_code="", error_message="", model_id="amazon.nova-canvas-v1:0")
@given(error_code=error_codes, error_message=error_messages,
       model_id=model_ids)
def test_classification_is_total_with_exact_branches(error_code,
                                                     error_message,
                                                     model_id):
    """Total: always a non-empty reason; AccessDenied -> model-access
    reason with the model id; ResourceNotFound+Legacy -> lifecycle
    reason; everything else -> code/message passthrough
    (Requirements 9.1, 9.2)."""
    reason = classify_bedrock_invocation_error(error_code, error_message,
                                               model_id)

    # Total: non-empty string for every input.
    assert isinstance(reason, str)
    assert reason.strip() != ""

    if error_code == "AccessDeniedException":
        # Identifies that Bedrock model access is not granted, and
        # contains the model id (Req 9.1).
        assert "access" in reason.lower()
        assert "not granted" in reason.lower()
        assert model_id in reason
    elif (error_code == "ResourceNotFoundException"
          and "legacy" in error_message.lower()):
        # Identifies the model's lifecycle status as the cause (Req 9.2).
        assert "legacy" in reason.lower()
        assert "lifecycle" in reason.lower()
        assert model_id in reason
    else:
        # Passthrough containing the original code and message.
        assert error_code in reason
        assert error_message in reason
