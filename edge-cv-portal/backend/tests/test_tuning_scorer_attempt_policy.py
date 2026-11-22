"""The Bedrock_Scorer replays an inspection-format node under the node's
attempt policy (``attempts``, ``attempt_timeout_s``, ``budget_s``), as the
device's executor does.

The real ``functions/workflow_tuning.py`` handler against moto, on the
harness of ``test_tuning_inspection_scoring.py``, with a Bedrock double that
plays a per-sample sequence of ``(duration_s, answer or exception)`` and
advances a fake ``replay_clock`` by each duration. One sample per run, so
the scorer's worker threads never share the clock.

The device cases of ``test/backend-test/workflow_engine/
test_bedrock_attempt_policy.py``, restated for the replay:

- the default policy makes one call through ``bedrock_client(region)``;
  any other policy builds the executor's policy client
  (``bedrock_client(region, attempt_timeout_s=...)``: connect 2 s, read the
  attempt timeout, one HTTP attempt per call);
- up to ``attempts`` calls; a raised error or a malformed answer is a failed
  attempt; ``ValidationException``, ``AccessDeniedException`` and
  ``ResourceNotFoundException`` end the loop; with a budget, attempt k >= 2
  starts only while the budget leaves a whole attempt timeout;
- the outcome is the last attempt's and records ``attempts``; the run
  records ``attemptPolicy`` and the step logs it;
- a legacy-format node keeps one call, with no ``attempts`` field.
"""
import logging
import threading

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from test_tuning_inspection_scoring import (  # noqa: F401 - tuning is a fixture
    ANOMALY,
    FAIL,
    PASS,
    Env,
    tuning,
)

ENDPOINT = "https://bedrock-runtime.example.invalid"
DEMO_POLICY = {"attempts": 2, "attempt_timeout_s": 6, "budget_s": 11}
LEGACY_OK = '{"is_anomalous": false, "confidence": 0.9}'


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "Converse")


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SequenceBedrock:
    """Plays ``script[execution id]``, a list of ``(duration_s, answer or
    exception)``: each call advances the clock by the duration, then returns
    the answer or raises. Records the execution id of every call."""

    def __init__(self, clock):
        self.clock = clock
        self.script = {}
        self.calls = []
        self._lock = threading.Lock()

    def converse(self, **kwargs):
        content = kwargs["messages"][0]["content"]
        marker = next(block["image"]["source"]["bytes"] for block in content
                      if "image" in block).decode("utf-8", "replace")
        execution = marker[len("INPUT-"):]
        with self._lock:
            self.calls.append(execution)
            steps = self.script.get(execution) or []
            if not steps:
                raise RuntimeError(f"unscripted call {len(self.calls)}")
            duration, answer = steps.pop(0)
            self.clock.advance(duration)
        if isinstance(answer, BaseException):
            raise answer
        return {"output": {"message": {"content": [{"text": answer}]}},
                "usage": {"outputTokens": 42}}


class PolicyEnv(Env):
    """The inspection-scoring Env with the sequence double, the fake clock
    and a client seam that records the options it was called with."""

    def __init__(self, stack, module, monkeypatch):
        super().__init__(stack, module, monkeypatch)
        self.clock = FakeClock()
        self.bedrock = SequenceBedrock(self.clock)
        self.clients = []
        monkeypatch.setattr(module, "bedrock_client", self._policy_client)
        monkeypatch.setattr(module, "replay_clock", self.clock)

    def _policy_client(self, region, **options):
        self.clients.append((region, options))
        return self.bedrock

    def score_one(self, parameters, script, label="OK"):
        """Score one sample once; returns ``(its outcome, the run view)``."""
        self.put_workflow(parameters)
        sample = self.put_sample("s1")
        self.open_session()
        self.label([sample["sampleId"]], label)
        self.bedrock.script["s1"] = list(script)
        status, body = self.call(
            "POST", f"{ANOMALY}/sessions/{{id}}/score-runs",
            {"id": self.session_id},
            body={"candidateId": "baseline", "repeats": 1})
        assert status == 202, body
        run_id = body["runId"]
        steps = 0
        while self.dispatched:
            steps += 1
            assert steps < 20, "the step chain did not terminate"
            self.module.handler(self.dispatched.pop(0), None)
        status, view = self.call(
            "GET", f"{ANOMALY}/score-runs/{{rid}}/outcomes", {"rid": run_id})
        assert status == 200, view
        assert view["run"]["status"] == "completed", view["run"]
        (outcome,) = view["outcomes"]
        return outcome, view["run"]


@pytest.fixture
def env(aws_stack, tuning, monkeypatch):
    return PolicyEnv(aws_stack, tuning, monkeypatch)


# ==========================================================================
# The client and the recorded policy
# ==========================================================================

def test_the_default_policy_is_one_call_through_todays_client(env):
    outcome, run = env.score_one({}, [(0.4, PASS)])
    assert env.clients == [("us-west-2", {})]
    assert run["attemptPolicy"] == {"attempts": 1, "attemptTimeoutS": 30,
                                    "budgetS": 0}
    assert env.bedrock.calls == ["s1"]
    assert (outcome["category"], outcome["attempts"]) == ("correct", 1)
    assert outcome["latencyMs"] == 400


def test_the_default_policy_does_not_retry_a_failure(env):
    outcome, _run = env.score_one({}, [(0.1, RuntimeError("boom")),
                                       (0.1, PASS)])
    assert env.bedrock.calls == ["s1"]
    assert outcome["category"] == "invocation_error"
    assert outcome["error"] == "RuntimeError: boom"
    assert outcome["attempts"] == 1


def test_a_policy_builds_the_executors_client_and_is_recorded(env, caplog):
    policy = {"attempts": 1, "attempt_timeout_s": 14, "budget_s": 14}
    with caplog.at_level(logging.INFO):
        outcome, run = env.score_one(policy, [(2.0, RuntimeError("boom")),
                                              (0.1, PASS)])
    assert env.clients == [("us-west-2", {"attempt_timeout_s": 14})]
    assert run["attemptPolicy"] == {"attempts": 1, "attemptTimeoutS": 14,
                                    "budgetS": 14}
    assert env.bedrock.calls == ["s1"]
    assert (outcome["category"], outcome["attempts"]) == ("invocation_error", 1)
    assert any("replays under the attempt policy attempts=1, "
               "attempt_timeout_s=14, budget_s=14" in record.getMessage()
               for record in caplog.records)


def test_the_real_policy_client_has_the_executors_settings(tuning):
    """The seam itself: connect 2 s, the attempt timeout as the read
    timeout, and one HTTP attempt per call (the device's policy client)."""
    client = tuning.bedrock_client("us-west-2", attempt_timeout_s=14)
    assert client.meta.region_name == "us-west-2"
    assert client.meta.config.connect_timeout == 2
    assert client.meta.config.read_timeout == 14
    assert client.meta.config.retries == {"total_max_attempts": 1,
                                          "mode": "standard"}
    default = tuning.bedrock_client("us-west-2")
    assert default.meta.config.read_timeout == 30
    assert default.meta.config.retries == {"total_max_attempts": 2,
                                           "mode": "legacy"}


# ==========================================================================
# The loop
# ==========================================================================

def test_a_fast_failure_is_retried_within_the_budget(env):
    outcome, _run = env.score_one(DEMO_POLICY, [(1.0, RuntimeError("boom")),
                                                (2.0, PASS)])
    assert env.bedrock.calls == ["s1", "s1"]
    assert env.clients == [("us-west-2", {"attempt_timeout_s": 6})]
    assert outcome["category"] == "correct"
    assert outcome["attempts"] == 2
    assert outcome["latencyMs"] == 3000
    assert outcome["rawAnswer"] == PASS
    assert outcome.get("error") is None


def test_a_slow_failure_leaving_less_than_the_attempt_timeout_is_not_retried(
        env):
    outcome, _run = env.score_one(
        DEMO_POLICY, [(6.0, ReadTimeoutError(endpoint_url=ENDPOINT)),
                      (1.0, PASS)])
    # 11 - 6 = 5 s left < the 6 s attempt timeout: no second attempt.
    assert env.bedrock.calls == ["s1"]
    assert outcome["category"] == "invocation_error"
    assert outcome["error"].startswith("ReadTimeoutError: ")
    assert outcome["attempts"] == 1
    assert outcome["latencyMs"] == 6000


def test_exactly_enough_budget_left_allows_the_retry(env):
    outcome, _run = env.score_one(DEMO_POLICY, [(5.0, RuntimeError("boom")),
                                                (1.0, PASS)])
    assert env.bedrock.calls == ["s1", "s1"]
    assert (outcome["category"], outcome["attempts"]) == ("correct", 2)


def test_a_malformed_answer_counts_as_an_attempt(env):
    outcome, _run = env.score_one(DEMO_POLICY, [(0.5, "nonsense"),
                                                (0.5, PASS)])
    assert env.bedrock.calls == ["s1", "s1"]
    assert (outcome["category"], outcome["attempts"]) == ("correct", 2)
    assert outcome["rawAnswer"] == PASS
    assert outcome.get("parseError") is None


def test_never_more_than_attempts_calls(env):
    script = [(0.1, RuntimeError(f"boom {i}")) for i in range(5)]
    outcome, _run = env.score_one({"attempts": 3, "attempt_timeout_s": 6},
                                  script)
    assert env.bedrock.calls == ["s1"] * 3
    assert outcome["category"] == "invocation_error"
    assert outcome["error"] == "RuntimeError: boom 2"
    assert outcome["attempts"] == 3


@pytest.mark.parametrize("code", ["ValidationException", "AccessDeniedException",
                                  "ResourceNotFoundException"])
def test_a_terminal_code_ends_the_loop_after_one_call(env, code):
    outcome, _run = env.score_one({"attempts": 3, "attempt_timeout_s": 6},
                                  [(0.1, client_error(code)), (0.1, PASS)])
    assert env.bedrock.calls == ["s1"]
    assert outcome["category"] == "invocation_error"
    assert outcome["error"].startswith("ClientError: ")
    assert code in outcome["error"]
    assert outcome["attempts"] == 1


def test_a_throttled_call_is_retried(env):
    outcome, _run = env.score_one(
        DEMO_POLICY, [(0.1, client_error("ThrottlingException")),
                      (0.1, PASS)])
    assert env.bedrock.calls == ["s1", "s1"]
    assert (outcome["category"], outcome["attempts"]) == ("correct", 2)


# ==========================================================================
# The outcome is the last attempt's
# ==========================================================================

def test_the_last_malformed_answer_is_the_outcome(env):
    outcome, _run = env.score_one(DEMO_POLICY, [(0.1, "first"),
                                                (0.1, "second")])
    assert outcome["category"] == "parse_failure"
    assert outcome["rawAnswer"] == "second"
    assert outcome["parseError"]
    assert outcome.get("error") is None
    assert outcome["attempts"] == 2


def test_a_last_call_that_raised_keeps_no_earlier_answer(env):
    outcome, _run = env.score_one(DEMO_POLICY, [(0.1, "nonsense"),
                                                (0.1, RuntimeError("late"))])
    assert outcome["category"] == "invocation_error"
    assert outcome["error"] == "RuntimeError: late"
    assert outcome.get("rawAnswer") is None
    assert outcome.get("parseError") is None
    assert outcome["attempts"] == 2


def test_a_last_accepted_answer_is_the_outcome(env):
    outcome, _run = env.score_one(DEMO_POLICY, [(0.1, RuntimeError("early")),
                                                (0.1, FAIL)], label="NOK")
    assert outcome["category"] == "correct"
    assert (outcome["verdict"], outcome["isAnomalous"]) == ("fail", True)
    assert outcome.get("error") is None
    assert outcome["attempts"] == 2


# ==========================================================================
# Legacy-format nodes are unchanged
# ==========================================================================

def test_a_legacy_node_keeps_one_call_and_records_no_attempts(env):
    outcome, run = env.score_one(
        {"answer_format": "", "attempts": 3, "attempt_timeout_s": 6},
        [(0.1, RuntimeError("boom")), (0.1, LEGACY_OK)])
    assert env.clients == [("us-west-2", {})]
    assert env.bedrock.calls == ["s1"]
    assert run["attemptPolicy"] is None
    assert outcome["category"] == "invocation_error"
    assert "attempts" not in outcome
