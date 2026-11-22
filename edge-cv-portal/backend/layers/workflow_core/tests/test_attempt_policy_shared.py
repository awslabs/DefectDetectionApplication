"""The attempt/budget policy primitives the executor and the Portal's
replay share: ``policy_int``, the catalog defaults, ``resolve_attempt_policy``
with its minimums and ``is_default``, and the terminal ``ClientError``
classification, which works by shape (no botocore import).
"""
import pytest

from workflow_core.anomaly_invocation import (
    BEDROCK_DEFAULT_ATTEMPT_TIMEOUT_SEC,
    BEDROCK_DEFAULT_ATTEMPTS,
    BEDROCK_DEFAULT_BUDGET_SEC,
    BEDROCK_READ_TIMEOUT_SEC,
    BEDROCK_TERMINAL_ERROR_CODES,
    AttemptPolicy,
    bedrock_error_code,
    is_terminal_bedrock_error,
    policy_int,
    resolve_attempt_policy,
)


class ClientErrorLike(Exception):
    """A botocore ``ClientError`` by shape: a ``response`` attribute."""

    def __init__(self, response):
        super().__init__("client error")
        self.response = response


def client_error(code):
    return ClientErrorLike({"Error": {"Code": code, "Message": "m"}})


class TestPolicyInt:
    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_absent_or_blank_is_the_default(self, raw):
        assert policy_int(raw, 7, 1) == 7

    @pytest.mark.parametrize("raw", ["abc", "2.5", [], {}, object()])
    def test_invalid_is_the_default(self, raw):
        assert policy_int(raw, 7, 1) == 7

    @pytest.mark.parametrize("raw, expected", [("3", 3), (" 4 ", 4), (5, 5), (2.9, 2)])
    def test_strings_and_numbers_are_integers(self, raw, expected):
        assert policy_int(raw, 7, 1) == expected

    @pytest.mark.parametrize("raw, minimum, expected", [
        (0, 1, 1), ("-3", 1, 1), (-1, 0, 0), ("0", 0, 0)])
    def test_never_below_the_minimum(self, raw, minimum, expected):
        assert policy_int(raw, 7, minimum) == expected


class TestResolveAttemptPolicy:
    def test_catalog_defaults(self):
        assert (BEDROCK_DEFAULT_ATTEMPTS, BEDROCK_DEFAULT_ATTEMPT_TIMEOUT_SEC,
                BEDROCK_DEFAULT_BUDGET_SEC) == (1, 30, 0)
        assert BEDROCK_DEFAULT_ATTEMPT_TIMEOUT_SEC == BEDROCK_READ_TIMEOUT_SEC

    def test_absent_parameters_are_the_default_policy(self):
        policy = resolve_attempt_policy({})
        assert policy == AttemptPolicy(1, 30, 0)
        assert policy.is_default

    def test_explicit_defaults_and_invalid_values_are_the_default_policy(self):
        assert resolve_attempt_policy(
            {"attempts": "1", "attempt_timeout_s": 30, "budget_s": "0"}).is_default
        assert resolve_attempt_policy(
            {"attempts": "x", "attempt_timeout_s": "", "budget_s": None}).is_default

    def test_a_policy_is_resolved_field_by_field(self):
        policy = resolve_attempt_policy(
            {"attempts": 2, "attempt_timeout_s": "6", "budget_s": 11})
        assert policy == AttemptPolicy(2, 6, 11)
        assert (policy.attempts, policy.attempt_timeout_s, policy.budget_s) == (2, 6, 11)
        assert not policy.is_default

    @pytest.mark.parametrize("parameters", [
        {"attempts": 2}, {"attempt_timeout_s": 14}, {"budget_s": 14}])
    def test_one_changed_parameter_is_not_the_default(self, parameters):
        assert not resolve_attempt_policy(parameters).is_default

    def test_minimums_are_one_attempt_one_second_and_no_budget(self):
        assert resolve_attempt_policy(
            {"attempts": 0, "attempt_timeout_s": 0, "budget_s": -5}
        ) == AttemptPolicy(1, 1, 0)


class TestTerminalErrors:
    def test_the_terminal_codes(self):
        assert BEDROCK_TERMINAL_ERROR_CODES == frozenset({
            "ValidationException", "AccessDeniedException",
            "ResourceNotFoundException"})

    @pytest.mark.parametrize("code", sorted(BEDROCK_TERMINAL_ERROR_CODES))
    def test_a_client_error_with_a_terminal_code_is_terminal(self, code):
        error = client_error(code)
        assert bedrock_error_code(error) == code
        assert is_terminal_bedrock_error(error)

    @pytest.mark.parametrize("code", ["ThrottlingException", "ModelTimeoutException",
                                      "SomethingNewException"])
    def test_other_codes_are_retryable(self, code):
        error = client_error(code)
        assert bedrock_error_code(error) == code
        assert not is_terminal_bedrock_error(error)

    def test_a_plain_exception_is_not_terminal(self):
        error = RuntimeError("ValidationException")
        assert bedrock_error_code(error) is None
        assert not is_terminal_bedrock_error(error)

    @pytest.mark.parametrize("response", [
        "ValidationException", None, {}, {"Error": "ValidationException"},
        {"Error": {}}, {"Error": {"Code": None}}])
    def test_malformed_responses_have_no_code(self, response):
        error = ClientErrorLike(response)
        assert bedrock_error_code(error) is None
        assert not is_terminal_bedrock_error(error)

    def test_a_code_is_read_as_text(self):
        assert bedrock_error_code(ClientErrorLike({"Error": {"Code": 404}})) == "404"
