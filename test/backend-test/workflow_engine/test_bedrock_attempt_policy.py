"""bedrock-inspection-chain: the attempt/budget policy of an
``answer_format: inspection`` node (design §3.5) and the client-kwargs
preservation of ``_default_bedrock_invoker``.

Fake invoker driven by a script and a fake monotonic clock injected into
``BedrockInferenceProcessor(clock=...)``: each scripted item advances the
clock by its duration, then returns the answer or raises. No network.
"""
import json
import os
import sys
import types

import cv2
import numpy as np
import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.node_status import NodeStatusCollector
from workflow_engine.output_bindings import (
    BEDROCK_CONNECT_TIMEOUT_SEC,
    BEDROCK_READ_TIMEOUT_SEC,
    BedrockInferenceError,
    BedrockInferenceProcessor,
    RunContext,
    _default_bedrock_invoker,
)

FRAME_NAME = "bedrock_frame_camera.jpg"
NODE_ID = "check"
GOOD = json.dumps({
    "verdict": "pass", "confidence": 0.9, "reason": "ok",
    "objects": [{"name": "roof", "qc": "OK", "reason": "r",
                 "bounding_box": {"x_min": 1, "y_min": 2, "x_max": 30, "y_max": 40}}],
})
ENDPOINT = "https://bedrock-runtime.example.invalid"
JPEG = b"\xff\xd8fake\xff\xd9"


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "Converse")


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ScriptedInvoker:
    """``script`` items are ``(duration_s, outcome)``; an outcome that is
    an exception is raised after the clock advanced."""

    def __init__(self, clock, *script):
        self.clock = clock
        self.script = list(script)
        self.calls = []

    def __call__(self, *args, **options):
        self.calls.append((args, options))
        duration, outcome = self.script.pop(0)
        self.clock.advance(duration)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def write_frame(directory):
    path = os.path.join(str(directory), FRAME_NAME)
    assert cv2.imwrite(path, np.zeros((48, 64, 3), dtype=np.uint8))
    return path


def make_binding(**parameters):
    params = {"prompt": "Inspect.", "answer_format": "inspection",
              "model": "us.amazon.nova-pro-v1:0", "on_error": "record"}
    params.update(parameters)
    return {
        "nodeId": NODE_ID, "binding": "bedrock_inference", "parameters": params,
        "upstreamNodeIds": ["cam"], "downstreamNodeIds": [],
        "capturePaths": {"in": "{work_dir}/" + FRAME_NAME, "reference": None},
    }


def run(tmp_path, clock, invoker, **parameters):
    write_frame(tmp_path)
    collector = NodeStatusCollector(extra_node_ids=[NODE_ID])
    context = RunContext(
        tag_values={"trigger": {"topic": "plant/line1/evaluate"}},
        output_dir=str(tmp_path), capture_id="cap-1", node_status=collector)
    processor = BedrockInferenceProcessor(invoker=invoker, clock=clock)
    document = {"schemaVersion": 1,
                "executorBindings": [make_binding(**parameters)]}
    metadata = processor.process(
        document, dict(context.tag_values), str(tmp_path), run_context=context)
    return metadata["bedrock"][NODE_ID]


DEMO_POLICY = {"attempts": 2, "attempt_timeout_s": 6, "budget_s": 11}
NON_DEFAULT_OPTIONS = {
    "timeouts": (BEDROCK_CONNECT_TIMEOUT_SEC, 6),
    "retries": {"total_max_attempts": 1, "mode": "standard"},
}


# ---------------------------------------------------------------------------
# The loop (§3.5)
# ---------------------------------------------------------------------------

class TestAttemptLoop:
    def test_fast_failure_is_retried_within_the_budget(self, tmp_path):
        clock = FakeClock()
        invoker = ScriptedInvoker(clock, (1.0, RuntimeError("boom")), (2.0, GOOD))
        entry = run(tmp_path, clock, invoker, **DEMO_POLICY)
        assert len(invoker.calls) == 2
        assert entry["outcome"] == "pass"
        assert entry["attempts"] == 2
        assert entry["latency_ms"] == 3000

    def test_slow_failure_leaving_less_than_attempt_timeout_is_not_retried(
            self, tmp_path):
        clock = FakeClock()
        invoker = ScriptedInvoker(
            clock, (6.0, ReadTimeoutError(endpoint_url=ENDPOINT)), (1.0, GOOD))
        entry = run(tmp_path, clock, invoker, **DEMO_POLICY)
        # 11 - 6 = 5 s left < 6 s attempt timeout: no second attempt.
        assert len(invoker.calls) == 1
        assert entry["outcome"] == "error"
        assert entry["attempts"] == 1
        assert entry["latency_ms"] == 6000
        assert entry["error"].startswith("ReadTimeoutError: ")

    def test_exactly_enough_budget_left_allows_the_retry(self, tmp_path):
        clock = FakeClock()
        invoker = ScriptedInvoker(clock, (5.0, RuntimeError("boom")), (1.0, GOOD))
        entry = run(tmp_path, clock, invoker, **DEMO_POLICY)
        assert len(invoker.calls) == 2 and entry["outcome"] == "pass"

    def test_malformed_answer_counts_as_an_attempt(self, tmp_path):
        clock = FakeClock()
        invoker = ScriptedInvoker(clock, (0.5, "nonsense"), (0.5, GOOD))
        entry = run(tmp_path, clock, invoker, **DEMO_POLICY)
        assert len(invoker.calls) == 2
        assert entry["outcome"] == "pass" and entry["attempts"] == 2
        assert entry["text"] == GOOD

    def test_never_more_than_attempts_calls(self, tmp_path):
        clock = FakeClock()
        invoker = ScriptedInvoker(
            clock, *[(0.1, RuntimeError("boom {0}".format(i))) for i in range(5)])
        entry = run(tmp_path, clock, invoker, attempts=3, attempt_timeout_s=6)
        assert len(invoker.calls) == 3
        assert entry["outcome"] == "error" and entry["attempts"] == 3
        assert entry["error"] == "RuntimeError: boom 2"

    def test_last_malformed_answer_is_the_recorded_text(self, tmp_path):
        clock = FakeClock()
        invoker = ScriptedInvoker(clock, (0.1, "first"), (0.1, "second"))
        entry = run(tmp_path, clock, invoker, **DEMO_POLICY)
        assert entry["outcome"] == "error" and entry["attempts"] == 2
        assert entry["error"].startswith("malformed Bedrock answer: ")
        assert entry["text"] == "second"


class TestClassification:
    @pytest.mark.parametrize("code", [
        "ValidationException", "AccessDeniedException", "ResourceNotFoundException"])
    def test_terminal_codes_end_the_loop_after_one_call(self, tmp_path, code):
        clock = FakeClock()
        invoker = ScriptedInvoker(clock, (0.1, client_error(code)), (0.1, GOOD))
        entry = run(tmp_path, clock, invoker, attempts=3, attempt_timeout_s=6)
        assert len(invoker.calls) == 1
        assert entry["outcome"] == "error" and entry["attempts"] == 1
        assert entry["error"].startswith("ClientError: ")
        assert code in entry["error"]

    @pytest.mark.parametrize("error", [
        client_error("ThrottlingException"),
        client_error("ServiceUnavailableException"),
        client_error("InternalServerException"),
        client_error("ModelTimeoutException"),
        client_error("ModelNotReadyException"),
        client_error("SomethingNewException"),
        ReadTimeoutError(endpoint_url=ENDPOINT),
        ConnectTimeoutError(endpoint_url=ENDPOINT),
        EndpointConnectionError(endpoint_url=ENDPOINT),
        OSError("socket closed"),
    ], ids=lambda e: type(e).__name__ + ":" + str(e)[:24])
    def test_retryable_failures_are_retried(self, tmp_path, error):
        clock = FakeClock()
        invoker = ScriptedInvoker(clock, (0.1, error), (0.1, GOOD))
        entry = run(tmp_path, clock, invoker, **DEMO_POLICY)
        assert len(invoker.calls) == 2
        assert entry["outcome"] == "pass"

    def test_failure_text_is_capped_at_300_characters(self, tmp_path):
        clock = FakeClock()
        invoker = ScriptedInvoker(clock, (0.1, RuntimeError("x" * 500)))
        entry = run(tmp_path, clock, invoker)
        assert entry["error"] == ("RuntimeError: " + "x" * 500)[:300]
        assert len(entry["error"]) == 300

    def test_fail_run_raises_after_the_exhausted_loop(self, tmp_path):
        clock = FakeClock()
        invoker = ScriptedInvoker(clock, (0.1, RuntimeError("a")), (0.1, RuntimeError("b")))
        with pytest.raises(BedrockInferenceError) as info:
            run(tmp_path, clock, invoker, on_error="fail_run", **DEMO_POLICY)
        assert len(invoker.calls) == 2
        assert str(info.value) == "Bedrock inference node 'check' failed: RuntimeError: b"


# ---------------------------------------------------------------------------
# Client options handed to the invoker (defaults → today's call)
# ---------------------------------------------------------------------------

class TestInvokerOptions:
    def test_defaults_call_the_invoker_exactly_as_today(self, tmp_path):
        clock = FakeClock()
        invoker = ScriptedInvoker(clock, (0.1, GOOD))
        run(tmp_path, clock, invoker)
        (args, options), = invoker.calls
        assert options == {}
        assert len(args) == 5
        assert args[0] == "us.amazon.nova-pro-v1:0" and args[3] == "us-east-1"

    def test_explicit_defaults_are_still_todays_call(self, tmp_path):
        clock = FakeClock()
        invoker = ScriptedInvoker(clock, (0.1, GOOD))
        run(tmp_path, clock, invoker, attempts=1, attempt_timeout_s=30, budget_s=0)
        assert invoker.calls[0][1] == {}

    def test_non_defaults_pass_timeouts_and_single_attempt_retries(self, tmp_path):
        clock = FakeClock()
        invoker = ScriptedInvoker(clock, (0.1, GOOD))
        run(tmp_path, clock, invoker, system_prompt="Be terse.", **DEMO_POLICY)
        (args, options), = invoker.calls
        assert options == NON_DEFAULT_OPTIONS
        assert args[5] == "Be terse."

    def test_a_single_changed_parameter_switches_the_client(self, tmp_path):
        clock = FakeClock()
        invoker = ScriptedInvoker(clock, (0.1, GOOD))
        run(tmp_path, clock, invoker, attempt_timeout_s=10)
        assert invoker.calls[0][1] == {
            "timeouts": (BEDROCK_CONNECT_TIMEOUT_SEC, 10),
            "retries": {"total_max_attempts": 1, "mode": "standard"},
        }


# ---------------------------------------------------------------------------
# _default_bedrock_invoker client kwargs (preservation + the new options)
# ---------------------------------------------------------------------------

class _StubConverseClient:
    def __init__(self):
        self.kwargs = []

    def converse(self, **kwargs):
        self.kwargs.append(kwargs)
        return {"output": {"message": {"content": [{"text": GOOD}]}}}


@pytest.fixture
def stub_boto3(monkeypatch):
    """A minimal ``boto3``/``botocore.config`` exposing what the transport
    built (the same shape ``test_anomaly_invocation_delegation`` uses)."""
    client = _StubConverseClient()
    captured = {}

    boto3 = types.ModuleType("boto3")

    def _client(service_name, region_name=None, config=None):
        captured["service_name"] = service_name
        captured["region_name"] = region_name
        captured["config"] = config
        return client

    boto3.client = _client
    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")

    class Config:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    botocore_config.Config = Config
    botocore.config = botocore_config
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config)
    return {"client": client, "captured": captured}


class TestDefaultInvokerClientKwargs:
    ARGS = ("model-x", "Inspect.", [("Input image", JPEG)], "us-west-2", 128)

    def test_defaults_build_todays_client(self, stub_boto3):
        answer = _default_bedrock_invoker(*self.ARGS)
        assert answer == GOOD
        captured = stub_boto3["captured"]
        assert captured["service_name"] == "bedrock-runtime"
        assert captured["region_name"] == "us-west-2"
        assert captured["config"].kwargs == {
            "read_timeout": BEDROCK_READ_TIMEOUT_SEC,
            "retries": {"max_attempts": 1},
        }

    def test_defaults_with_system_prompt_build_todays_client(self, stub_boto3):
        _default_bedrock_invoker(*self.ARGS, "Be terse.")
        assert stub_boto3["captured"]["config"].kwargs == {
            "read_timeout": BEDROCK_READ_TIMEOUT_SEC,
            "retries": {"max_attempts": 1},
        }
        assert stub_boto3["client"].kwargs[0]["system"] == [{"text": "Be terse."}]

    def test_policy_options_set_connect_read_timeouts_and_one_attempt(self, stub_boto3):
        _default_bedrock_invoker(*self.ARGS, None, **NON_DEFAULT_OPTIONS)
        assert stub_boto3["captured"]["config"].kwargs == {
            "connect_timeout": BEDROCK_CONNECT_TIMEOUT_SEC,
            "read_timeout": 6,
            "retries": {"total_max_attempts": 1, "mode": "standard"},
        }
        assert "system" not in stub_boto3["client"].kwargs[0]

    def test_policy_options_as_keywords_beside_system_prompt(self, stub_boto3):
        _default_bedrock_invoker(
            *self.ARGS, system_prompt="Be terse.", **NON_DEFAULT_OPTIONS)
        assert stub_boto3["captured"]["config"].kwargs["read_timeout"] == 6
        assert stub_boto3["client"].kwargs[0]["system"] == [{"text": "Be terse."}]
