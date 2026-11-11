"""
Unit tests: bedrock_common.send_dropping_refused_options.

A model can refuse a request option the Portal sets while it accepts the
rest of the request. Claude Opus 5.5 refuses a forced tool choice, a
temperature and a top_p with a ValidationException (messages below, as
Bedrock returned them on 2026-11-09). Every model refuses a maxTokens
above its maximum output with a ValidationException that states the
maximum (max_tokens_refusal, as Bedrock returned it on 2026-11-11). The
helper always sends the caller's request unchanged first, and sends it
again only without an option such a refusal names, or once with the
stated maximum. Every other outcome reaches the caller as before. The
per-caller tests live with each caller's suite.
"""
import copy
import logging
import re

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

import bedrock_common

TOOL_CHOICE_REFUSAL = ('The model returned the following errors: tool_choice: '
                       'type "tool" and "any" are not supported for this model.')
TEMPERATURE_REFUSAL = ('The model returned the following errors: `temperature` '
                       'is deprecated for this model.')
TOP_P_REFUSAL = ('The model returned the following errors: `top_p` is '
                 'deprecated for this model.')


def max_tokens_refusal(limit):
    """Bedrock's refusal of a maxTokens above the model's maximum output
    (Amazon Nova Pro's answer to 128000 states 10000)."""
    return (f"The maximum tokens you requested exceeds the model limit of "
            f"{limit}. Try again with a maximum tokens value that is lower "
            f"than {limit}.")


def client_error(code, message):
    return ClientError({"Error": {"Code": code, "Message": message}},
                       "Converse")


def forced_request(*, max_tokens=4096, **sampling):
    """A request shaped like the generators' (one forced tool)."""
    return {
        "modelId": "global.anthropic.claude-opus-5-5",
        "system": [{"text": "Always call create_workflow."}],
        "messages": [{"role": "user", "content": [{"text": "A pipeline"}]}],
        "inferenceConfig": {"maxTokens": max_tokens, **sampling},
        "toolConfig": {
            "tools": [{"toolSpec": {"name": "create_workflow",
                                    "inputSchema": {"json": {"type": "object"}}}}],
            "toolChoice": {"tool": {"name": "create_workflow"}},
        },
    }


class ScriptedSend:
    """A stand-in for client.converse: records every request and answers
    each with the next scripted outcome (a response, or an exception)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, **request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def without(request, section, key):
    expected = copy.deepcopy(request)
    del expected[section][key]
    return expected


def test_accepted_request_is_sent_once_unchanged():
    """A model that accepts the options gets exactly the caller's request,
    once."""
    request = forced_request(temperature=0.2)
    send = ScriptedSend({"stopReason": "tool_use"})

    response = bedrock_common.send_dropping_refused_options(send, request)

    assert response == {"stopReason": "tool_use"}
    assert send.requests == [forced_request(temperature=0.2)]


def test_refused_tool_choice_is_resent_with_the_tool_still_offered():
    request = forced_request()
    send = ScriptedSend(client_error("ValidationException", TOOL_CHOICE_REFUSAL),
                        {"stopReason": "tool_use"})

    response = bedrock_common.send_dropping_refused_options(send, request)

    assert response == {"stopReason": "tool_use"}
    assert send.requests[0] == forced_request()
    assert send.requests[1] == without(forced_request(), "toolConfig", "toolChoice")
    assert send.requests[1]["toolConfig"]["tools"] == request["toolConfig"]["tools"]


@pytest.mark.parametrize("sampling, message, key", [
    ({"temperature": 0.2}, TEMPERATURE_REFUSAL, "temperature"),
    ({"topP": 0.9}, TOP_P_REFUSAL, "topP"),
])
def test_refused_sampling_option_is_resent_without_it(sampling, message, key):
    """Only the refused option goes: maxTokens and the forced tool stay."""
    request = forced_request(**sampling)
    send = ScriptedSend(client_error("ValidationException", message),
                        {"stopReason": "tool_use"})

    bedrock_common.send_dropping_refused_options(send, request)

    assert len(send.requests) == 2
    assert send.requests[1] == without(forced_request(**sampling),
                                       "inferenceConfig", key)
    assert send.requests[1]["inferenceConfig"] == {"maxTokens": 4096}


def test_each_refusal_drops_one_more_option():
    """Opus 5.5 names one refused option per answer: the forced choice
    first, then the temperature; the third request carries neither."""
    send = ScriptedSend(client_error("ValidationException", TOOL_CHOICE_REFUSAL),
                        client_error("ValidationException", TEMPERATURE_REFUSAL),
                        {"stopReason": "tool_use"})

    response = bedrock_common.send_dropping_refused_options(
        send, forced_request(temperature=0.2))

    assert response == {"stopReason": "tool_use"}
    assert [("toolChoice" in r["toolConfig"], "temperature" in r["inferenceConfig"])
            for r in send.requests] == [(True, True), (False, True), (False, False)]
    assert send.requests[2]["inferenceConfig"] == {"maxTokens": 4096}


def test_one_refusal_naming_two_options_drops_both():
    send = ScriptedSend(
        client_error("ValidationException",
                     "toolConfig.toolChoice and temperature are not supported"),
        {"stopReason": "end_turn"})

    bedrock_common.send_dropping_refused_options(send, forced_request(temperature=0.2))

    assert len(send.requests) == 2
    assert "toolChoice" not in send.requests[1]["toolConfig"]
    assert send.requests[1]["inferenceConfig"] == {"maxTokens": 4096}


def test_refusal_of_an_option_the_request_lacks_is_raised():
    """Nothing to drop: the error reaches the caller after one request."""
    error = client_error("ValidationException", TEMPERATURE_REFUSAL)
    send = ScriptedSend(error)

    with pytest.raises(ClientError) as info:
        bedrock_common.send_dropping_refused_options(send, forced_request())

    assert info.value is error
    assert len(send.requests) == 1


@pytest.mark.parametrize("error", [
    client_error("ValidationException", "maxTokens too large"),
    # A word that only contains "top p" letters is not a refusal of topP.
    client_error("ValidationException", "invalid stopping criteria"),
    client_error("ThrottlingException", TOOL_CHOICE_REFUSAL),
    client_error("AccessDeniedException", "not authorized to set top_p"),
    ReadTimeoutError(endpoint_url="https://bedrock.test"),
    RuntimeError("boom"),
])
def test_other_errors_reach_the_caller_unchanged(error):
    send = ScriptedSend(error)

    with pytest.raises(type(error)) as info:
        bedrock_common.send_dropping_refused_options(
            send, forced_request(topP=0.9))

    assert info.value is error
    assert len(send.requests) == 1


def test_an_error_of_the_resend_reaches_the_caller():
    throttled = client_error("ThrottlingException", "slow down")
    send = ScriptedSend(client_error("ValidationException", TOOL_CHOICE_REFUSAL),
                        throttled)

    with pytest.raises(ClientError) as info:
        bedrock_common.send_dropping_refused_options(send, forced_request())

    assert info.value is throttled
    assert len(send.requests) == 2


def test_the_callers_request_is_not_modified():
    request = forced_request(temperature=0.2)
    snapshot = copy.deepcopy(request)
    send = ScriptedSend(client_error("ValidationException", TOOL_CHOICE_REFUSAL),
                        client_error("ValidationException", TEMPERATURE_REFUSAL),
                        {"stopReason": "tool_use"})

    bedrock_common.send_dropping_refused_options(send, request)

    assert request == snapshot


def test_a_request_without_options_is_sent_as_is():
    """The scorer's request shape (no tools, maxTokens only) passes
    straight through."""
    request = {"modelId": "m", "messages": [{"role": "user", "content": [{"text": "x"}]}],
               "inferenceConfig": {"maxTokens": 256}}
    send = ScriptedSend({"stopReason": "end_turn"})

    bedrock_common.send_dropping_refused_options(send, request)

    assert send.requests == [request]


class LimitedModel:
    """client.converse of a model whose maximum output is ``limit``: it
    refuses a larger maxTokens as Bedrock does and accepts the rest."""

    def __init__(self, limit):
        self.limit = limit
        self.requests = []

    def __call__(self, **request):
        self.requests.append(copy.deepcopy(request))
        if request["inferenceConfig"]["maxTokens"] > self.limit:
            raise client_error("ValidationException", max_tokens_refusal(self.limit))
        return {"stopReason": "end_turn"}


def test_max_tokens_above_the_model_limit_is_sent_again_with_the_limit():
    """128000 to a model whose maximum is 10000: one more request, equal
    to the first except maxTokens; the forced tool and the temperature
    stay."""
    send = ScriptedSend(client_error("ValidationException", max_tokens_refusal(10000)),
                        {"stopReason": "tool_use"})

    response = bedrock_common.send_dropping_refused_options(
        send, forced_request(max_tokens=128000, temperature=0.2))

    assert response == {"stopReason": "tool_use"}
    assert send.requests == [forced_request(max_tokens=128000, temperature=0.2),
                             forced_request(max_tokens=10000, temperature=0.2)]


@pytest.mark.parametrize("max_tokens, limit", [
    (10000, 10000),
    (4096, 10000),
    (128000, 128000),
])
def test_max_tokens_within_the_model_limit_is_sent_once_unchanged(max_tokens, limit):
    send = LimitedModel(limit)

    response = bedrock_common.send_dropping_refused_options(
        send, forced_request(max_tokens=max_tokens))

    assert response == {"stopReason": "end_turn"}
    assert send.requests == [forced_request(max_tokens=max_tokens)]


def test_a_second_max_tokens_refusal_reaches_the_caller():
    """At most one lowering per call: a model that refuses the stated
    maximum too (one whose limit also counts the prompt) gets 2 requests
    and the caller gets the second error."""
    second = client_error("ValidationException", max_tokens_refusal(8192))
    send = ScriptedSend(client_error("ValidationException", max_tokens_refusal(10000)),
                        second)

    with pytest.raises(ClientError) as info:
        bedrock_common.send_dropping_refused_options(
            send, forced_request(max_tokens=128000))

    assert info.value is second
    assert [r["inferenceConfig"]["maxTokens"] for r in send.requests] == [128000, 10000]


@pytest.mark.parametrize("stated", [10000, 4096, 0])
def test_a_stated_limit_not_below_the_request_is_raised(stated):
    error = client_error("ValidationException", max_tokens_refusal(stated))
    send = ScriptedSend(error)

    with pytest.raises(ClientError) as info:
        bedrock_common.send_dropping_refused_options(send, forced_request(max_tokens=4096))

    assert info.value is error
    assert len(send.requests) == 1


@pytest.mark.parametrize("code, message", [
    ("ValidationException", max_tokens_refusal("10,000")),
    ("ValidationException", max_tokens_refusal("10.000")),
    ("ValidationException", "maxTokens too large"),
    ("ThrottlingException", max_tokens_refusal(10000)),
])
def test_a_refusal_without_a_readable_limit_is_raised(code, message):
    """Nothing is lowered unless a ValidationException states the limit as
    a plain number."""
    error = client_error(code, message)
    send = ScriptedSend(error)

    with pytest.raises(ClientError) as info:
        bedrock_common.send_dropping_refused_options(send, forced_request(max_tokens=128000))

    assert info.value is error
    assert len(send.requests) == 1


def test_max_tokens_and_option_refusals_are_handled_in_turn():
    """Opus 5.5 checks maxTokens first: 200000 is refused stating 128000,
    then the forced tool choice and the temperature are refused one by
    one. 4 requests, the last one accepted."""
    send = ScriptedSend(client_error("ValidationException", max_tokens_refusal(128000)),
                        client_error("ValidationException", TOOL_CHOICE_REFUSAL),
                        client_error("ValidationException", TEMPERATURE_REFUSAL),
                        {"stopReason": "tool_use"})

    response = bedrock_common.send_dropping_refused_options(
        send, forced_request(max_tokens=200000, temperature=0.2))

    assert response == {"stopReason": "tool_use"}
    assert [(r["inferenceConfig"]["maxTokens"], "toolChoice" in r["toolConfig"],
             "temperature" in r["inferenceConfig"]) for r in send.requests] == [
        (200000, True, True), (128000, True, True), (128000, False, True),
        (128000, False, False)]


def test_without_lowering_a_max_tokens_refusal_is_raised():
    error = client_error("ValidationException", max_tokens_refusal(10000))
    send = ScriptedSend(error)

    with pytest.raises(ClientError) as info:
        bedrock_common.send_dropping_refused_options(
            send, forced_request(max_tokens=128000), lower_max_tokens=False)

    assert info.value is error
    assert len(send.requests) == 1


def test_without_lowering_a_refused_sampling_option_is_still_dropped():
    send = ScriptedSend(client_error("ValidationException", TEMPERATURE_REFUSAL),
                        {"stopReason": "end_turn"})

    bedrock_common.send_dropping_refused_options(
        send, forced_request(temperature=0.2), lower_max_tokens=False)

    assert send.requests == [forced_request(temperature=0.2), forced_request()]


@pytest.mark.parametrize("inference_config", [None, {}, {"maxTokens": 128000.0},
                                              {"maxTokens": True}])
def test_a_request_without_an_integer_max_tokens_is_raised(inference_config):
    request = {"modelId": "m", "messages": [{"role": "user", "content": [{"text": "x"}]}]}
    if inference_config is not None:
        request["inferenceConfig"] = inference_config
    error = client_error("ValidationException", max_tokens_refusal(10000))
    send = ScriptedSend(error)

    with pytest.raises(ClientError) as info:
        bedrock_common.send_dropping_refused_options(send, request)

    assert info.value is error
    assert len(send.requests) == 1


def test_the_lowering_is_logged(caplog):
    caplog.set_level(logging.WARNING)
    send = LimitedModel(10000)
    request = forced_request(max_tokens=128000)
    request["modelId"] = "us.amazon.nova-pro-v1:0"

    bedrock_common.send_dropping_refused_options(send, request)

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "us.amazon.nova-pro-v1:0" in warnings[0]
    assert {"128000", "10000"} <= set(re.findall(r"\d+", warnings[0]))


def test_the_callers_request_is_not_modified_by_a_lowering():
    request = forced_request(max_tokens=128000, temperature=0.2)
    snapshot = copy.deepcopy(request)
    send = ScriptedSend(client_error("ValidationException", max_tokens_refusal(10000)),
                        client_error("ValidationException", TEMPERATURE_REFUSAL),
                        {"stopReason": "tool_use"})

    bedrock_common.send_dropping_refused_options(send, request)

    assert request == snapshot
    assert send.requests[2]["inferenceConfig"] == {"maxTokens": 10000}
