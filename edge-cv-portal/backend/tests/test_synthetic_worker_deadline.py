"""The synthetic generation worker against the Lambda timeout
(fix-synthetic-worker-deadline).

Live facts this file encodes: a variation costs about 25 s (20 s of model
time plus 3-5 s of image work), so the 50-task plan of session 6115b94c
(10 sources x 5 variations) needs about 20 minutes against the worker's
900 s timeout. Pass 2 of that session ran 43 of 50 tasks, timed out, and
Lambda's default asynchronous retry policy re-ran the whole plan from task
0 twice: 130 failed PREVIEW items for 50 tasks, all with the same seeds,
and a session stuck in `generating` because the worker only reports
`awaiting_review` after the last task.

Pure part (no AWS): the hand-over threshold, the task identity of a
preview record, the remaining-task computation a continuation runs, the
staleness predicate the read path applies, and the property that a chain
of continuations writes every task exactly once.

Worker part (moto stack from conftest.py + synthetic_env.py): a fake clock
plus a bedrock-runtime stub that costs 25 s per call drive the worker
through a 900 s invocation (32 tasks, then a hand-over to a continuation
that finishes the remaining 18), a killed invocation whose redelivered
event must not duplicate previews, and the reaper that moves a dead pass
to `failed`. Route part: `generate` answers 409 while a pass is running
and starts a new pass over a stale one; the 6115b94c shape (130 failed
previews of pass 2, no worker fields) is reaped on GET and regenerates to
`awaiting_review` with exactly 50 pass-3 previews.
"""
import base64
import io
import json
import math
import os
import sys
import uuid

import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "functions"))

from synthetic_env import SyntheticEnv  # noqa: E402

TASK_MS = 25_000  # measured: ~20 s model time + 3-5 s image work
HOUR_MS = 3_600_000


@pytest.fixture(scope="module")
def core():
    """synthetic_core imported lazily (after any fake ``shared_utils`` a
    standalone test installed at collection time has been dropped), the
    same way test_property_synthetic_worker_failures.py binds its seams."""
    shared = sys.modules.get("shared_utils")
    if shared is not None and not hasattr(shared, "check_user_access"):
        sys.modules.pop("shared_utils", None)
        sys.modules.pop("synthetic_data", None)
    import synthetic_core
    return synthetic_core


# ======================================================== pure helpers

def _plan(core, sources, variation_count):
    return core.build_generation_plan(
        {"generation_model_id": "amazon.nova-canvas-v1:0"},
        [{"bucket": "b", "key": key} for key in sources],
        variation_count, "a prompt", {"seed": 11})


def _preview_for(task, generation_pass, with_index=True):
    preview = {
        "generation_pass": generation_pass,
        "source_image_key": task["source_image"]["key"],
        "variation_index": task["variation_index"],
    }
    if with_index:
        preview["task_index"] = task["task_index"]
    return preview


def test_should_hand_over_exactly_below_the_reserve(core):
    reserve = core.CONTINUATION_RESERVE_MS
    assert core.should_hand_over(reserve) is False
    assert core.should_hand_over(reserve - 1) is True
    assert core.should_hand_over(0) is True


def test_generation_is_stale_ignores_sessions_that_are_not_generating(core):
    now = 1_700_000_000_000
    for status in ("draft", "awaiting_review", "approved", "integrated",
                   "failed"):
        meta = {"status": status, "updated_at": now - 48 * HOUR_MS}
        assert core.generation_is_stale(meta, now, 900_000) is False


def test_generation_is_stale_uses_the_recorded_deadline_plus_grace(core):
    deadline = 1_700_000_000_000
    meta = {"status": "generating", "worker_deadline_at": deadline,
            "updated_at": deadline - 48 * HOUR_MS}
    boundary = deadline + core.STALE_WORKER_GRACE_MS
    assert core.generation_is_stale(meta, boundary, 900_000) is False
    assert core.generation_is_stale(meta, boundary + 1, 900_000) is True


def test_generation_is_stale_falls_back_to_updated_at_for_the_stuck_shape(
        core):
    """Session 6115b94c's META: `generating`, `updated_at` hours old, no
    worker fields at all."""
    now = 1_700_000_000_000
    stuck = {"status": "generating", "generation_pass": 2,
             "updated_at": now - 4 * HOUR_MS}
    assert core.generation_is_stale(stuck, now, 900_000) is True
    fresh = dict(stuck, updated_at=now)
    assert core.generation_is_stale(fresh, now, 900_000) is False
    assert core.worker_deadline_ms(fresh, 900_000) == now + 900_000


def test_remaining_tasks_is_the_whole_plan_without_previews(core):
    plan = _plan(core, ["a.png", "b.png"], 3)
    assert core.remaining_tasks(plan, [], 1) == plan


def test_remaining_tasks_ignores_previews_of_other_passes(core):
    plan = _plan(core, ["a.png", "b.png"], 2)
    other_pass = [_preview_for(task, 1) for task in plan]
    assert core.remaining_tasks(plan, other_pass, 2) == plan
    assert core.remaining_tasks(plan, other_pass, 1) == []


def test_remaining_tasks_matches_a_legacy_preview_by_key_and_variation(
        core):
    """A record written before task indexes were stored still counts."""
    plan = _plan(core, ["a.png", "b.png"], 2)
    legacy = [_preview_for(plan[1], 1, with_index=False)]
    assert core.remaining_tasks(plan, legacy, 1) == [plan[0], plan[2],
                                                     plan[3]]
    assert core.preview_task_identity(legacy[0]) == ("key", "a.png", 1)
    assert core.preview_task_identity(_preview_for(plan[1], 1)) == (
        "index", 1)


def test_failure_and_running_messages_name_the_pass_and_counts(core):
    reason = core.stale_generation_failure_reason(2, 130, 50)
    assert "pass 2" in reason and "130" in reason and "50" in reason
    assert "Regenerate" in reason
    message = core.running_generation_message(1, 7, 50)
    assert "already running" in message and "7 of 50" in message


@settings(deadline=None)
@given(
    sources=st.lists(st.from_regex(r"datasets/[a-z0-9]{1,8}\.png",
                                   fullmatch=True),
                     min_size=1, max_size=6, unique=True),
    variation_count=st.integers(min_value=1, max_value=5),
    capacity=st.integers(min_value=1, max_value=8),
    crash_index=st.none() | st.integers(min_value=0, max_value=29),
)
def test_chain_of_continuations_writes_every_task_exactly_once(
        core, sources, variation_count, capacity, crash_index):
    """**Feature: fix-synthetic-worker-deadline, Property 1: a chain of
    continuations writes every task exactly once**

    Each hop runs `remaining_tasks` over the records written so far and
    writes `capacity` of them. A crash inside a task (its record never
    written) is modelled by dropping one write: the next hop redoes exactly
    that task. The chain ends after ceil(n / capacity) hops (plus one for
    the crash), every task index appears exactly once, in plan order, and
    nothing remains."""
    plan = _plan(core, sources, variation_count)
    generation_pass = 3
    written = []
    hops = 0
    crashed = False
    while True:
        rest = core.remaining_tasks(plan, written, generation_pass)
        if not rest:
            break
        hops += 1
        batch = rest[:capacity]
        for task in batch:
            if (not crashed and crash_index is not None
                    and task["task_index"] == crash_index):
                crashed = True  # the record of this task is lost
                continue
            written.append(_preview_for(task, generation_pass))
        assert hops <= math.ceil(len(plan) / capacity) + 1
    indexes = [p["task_index"] for p in written]
    assert sorted(indexes) == list(range(len(plan)))
    assert len(set(indexes)) == len(plan)
    assert core.remaining_tasks(plan, written, generation_pass) == []
    if crash_index is None or crash_index >= len(plan):
        assert hops == math.ceil(len(plan) / capacity)
        assert indexes == list(range(len(plan)))


# ======================================================= worker (moto)

@pytest.fixture(scope="module")
def senv(aws_stack):
    return SyntheticEnv(aws_stack)


@pytest.fixture
def actor(senv):
    """(usecase_id, DataScientist user) pair, fresh per test."""
    usecase_id = senv.create_usecase()
    return usecase_id, senv.actor_with_role(usecase_id, "DataScientist")


def _patched(senv, name, replacement):
    """Context-manager-free patch helper: returns a restore callable."""
    sd = senv.synthetic_data
    original = getattr(sd, name)
    setattr(sd, name, replacement)
    return lambda: setattr(sd, name, original)


def _png_bytes(width, height):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 120, 120)).save(
        buffer, format="PNG")
    return buffer.getvalue()


class _Clock:
    """Fake wall clock the handler's ``_now_ms`` and the Lambda context
    read; the bedrock stub advances it by TASK_MS per call."""

    def __init__(self, now_ms=1_700_000_000_000):
        self.now_ms = now_ms

    def advance(self, ms):
        self.now_ms += ms

    def __call__(self):
        return self.now_ms


class _LambdaContext:
    def __init__(self, clock, timeout_ms):
        self._clock = clock
        self.deadline = clock.now_ms + timeout_ms

    def get_remaining_time_in_millis(self):
        return max(0, self.deadline - self._clock.now_ms)


class _LambdaKilled(BaseException):
    """The runtime ending the invocation at its timeout: not an Exception,
    so the worker's per-task isolation cannot catch it, exactly as a real
    timeout cannot be caught."""


class _TimedBedrockRuntime:
    """bedrock-runtime stub: each call costs TASK_MS on the clock, kills
    the invocation when the bound context has no time left (or at
    ``kill_at_call``), and otherwise answers a 64x48 PNG."""

    def __init__(self, clock, context=None, kill_at_call=None):
        self.clock = clock
        self.context = context
        self.kill_at_call = kill_at_call
        self.calls = []

    def invoke_model(self, **kwargs):
        self.calls.append(kwargs["modelId"])
        self.clock.advance(TASK_MS)
        if (self.context is not None
                and self.context.get_remaining_time_in_millis() <= 0):
            raise _LambdaKilled(
                f"Task timed out after call {len(self.calls)}")
        if self.kill_at_call is not None and \
                len(self.calls) == self.kill_at_call:
            raise _LambdaKilled(f"killed at call {len(self.calls)}")
        payload = {"images": [base64.b64encode(_png_bytes(64, 48)).decode()]}
        return {"body": io.BytesIO(json.dumps(payload).encode())}


def _sources_for(senv, session_id, count):
    """``count`` in-limit (320x320) Nova Canvas sources uploaded to the
    data bucket; returns the Source_Image references."""
    sources = []
    for index in range(count):
        key = f"datasets/deadline/{session_id}/{index}.png"
        senv.s3.put_object(Bucket=senv.bucket, Key=key,
                           Body=_png_bytes(320, 320))
        sources.append({"bucket": senv.bucket, "key": key})
    return sources


def _start_pass(senv, user, usecase_id, sources=10, variations=5):
    """A draft session with ``sources`` x ``variations`` tasks whose
    generate request is accepted; ``_invoke_worker_async`` must already be
    patched by the caller to capture the worker payload(s)."""
    session_id = str(uuid.uuid4())
    senv.put_session_meta(
        usecase_id, session_id=session_id, status="draft",
        generation_model_id="amazon.nova-canvas-v1:0",
        source_class="defect",
        source_images=_sources_for(senv, session_id, sources),
        generation_params={"variation_count": variations},
    )
    status, body = senv.invoke(
        "POST", "/synthetic/sessions/{id}/generate", user, session_id, {})
    assert status == 202, body
    return session_id


def _items(senv, session_id):
    return senv.synthetic_data._from_ddb(senv.sessions_table.query(
        KeyConditionExpression="session_id = :s",
        ExpressionAttributeValues={":s": session_id})["Items"])


def _previews(senv, session_id):
    return [i for i in _items(senv, session_id)
            if str(i["sk"]).startswith("PREVIEW#")]


def _meta(senv, session_id):
    return next(i for i in _items(senv, session_id) if i["sk"] == "META")


def _run_worker(senv, payload, runtime, context=None):
    restore = _patched(senv, "_bedrock_runtime", runtime)
    try:
        if context is None:
            return senv.synthetic_data.run_generation_worker(payload)
        return senv.synthetic_data.run_generation_worker(payload, context)
    finally:
        restore()


def _distinct_pairs(previews):
    by_index = {(p["generation_pass"], p.get("task_index"))
                for p in previews}
    by_key = {(p["source_image_key"], p["variation_index"])
              for p in previews}
    return by_index, by_key


def _per_invocation(core, timeout_ms, tasks):
    """Tasks a ``timeout_ms`` invocation starts before the reserve."""
    return len([k for k in range(tasks)
                if timeout_ms - k * TASK_MS >= core.CONTINUATION_RESERVE_MS])


class TestWorkerDeadline:
    def test_fifty_task_pass_completes_across_continuations_without_duplicates(
            self, senv, actor, core):
        """The user's acceptance test: 10 sources x 5 variations at 25 s a
        task. The first 900 s invocation runs 32 tasks and hands the rest to
        a continuation; the continuation runs the other 18 and reports
        `awaiting_review`; 50 previews, no duplicate task."""
        usecase_id, user = actor
        clock = _Clock()
        payloads = []
        restores = [_patched(senv, "_now_ms", clock),
                    _patched(senv, "_invoke_worker_async", payloads.append)]
        try:
            session_id = _start_pass(senv, user, usecase_id)
            assert len(payloads) == 1
            assert payloads[0]["worker_token"]
            plan = _meta(senv, session_id)["generation_plan"]
            assert len(plan) == 50
            expected_first = _per_invocation(core, 900_000, 50)
            assert expected_first == 32

            context = _LambdaContext(clock, 900_000)
            result = _run_worker(senv, payloads[0],
                                 _TimedBedrockRuntime(clock, context), context)
            assert result["status"] == "continued", result
            assert result["completed"] == expected_first
            assert result["remaining"] == 50 - expected_first
            assert len(_previews(senv, session_id)) == expected_first
            meta = _meta(senv, session_id)
            assert meta["status"] == "generating"
            assert len(payloads) == 2, payloads
            assert payloads[1]["generation_pass"] == 1
            assert payloads[1]["continuation"] == 1
            assert payloads[1]["session_id"] == session_id
            assert meta["worker_token"] == payloads[1]["worker_token"]
            assert payloads[1]["worker_token"] != payloads[0]["worker_token"]
            assert meta["worker_deadline_at"] == \
                clock.now_ms + senv.synthetic_data.WORKER_TIMEOUT_MS

            context = _LambdaContext(clock, 900_000)
            result = _run_worker(senv, payloads[1],
                                 _TimedBedrockRuntime(clock, context), context)
            assert result["status"] == "done", result
            assert result["completed"] == 50 - expected_first
            previews = _previews(senv, session_id)
            assert len(previews) == 50
            by_index, by_key = _distinct_pairs(previews)
            assert len(by_index) == 50 and len(by_key) == 50
            assert all(p["status"] == "completed" for p in previews)
            seeds = {p["task_index"]: p["seed"] for p in previews}
            assert seeds == {t["task_index"]: t["seed"] for t in plan}
            assert _meta(senv, session_id)["status"] == "awaiting_review"
            assert len(payloads) == 2
        finally:
            for restore in restores:
                restore()

    def test_redelivered_event_after_a_killed_invocation_does_not_duplicate(
            self, senv, actor, core):
        """The pre-fix reproduction. The runtime kills the invocation at
        its 36th call (35 previews written, status still `generating`);
        Lambda's retry then delivers the same event again. Before the fix
        the retry re-ran the plan from task 0 (85 previews); now it is
        rejected as stale. Later the read path reaps the dead pass to
        `failed` and a new pass can start."""
        usecase_id, user = actor
        clock = _Clock()
        payloads = []
        restores = [_patched(senv, "_now_ms", clock),
                    _patched(senv, "_invoke_worker_async", payloads.append)]
        try:
            session_id = _start_pass(senv, user, usecase_id)
            with pytest.raises(_LambdaKilled):
                _run_worker(senv, payloads[0],
                            _TimedBedrockRuntime(clock, kill_at_call=36))
            assert len(_previews(senv, session_id)) == 35
            assert _meta(senv, session_id)["status"] == "generating"

            # Lambda's asynchronous retry: the very same event again.
            result = _run_worker(senv, payloads[0], _TimedBedrockRuntime(clock))
            previews = _previews(senv, session_id)
            assert len(previews) == 35, (
                f"the retried event duplicated previews: {len(previews)}")
            assert result["status"] == "stale", result
            meta = _meta(senv, session_id)
            assert meta["status"] == "generating"
            assert len(payloads) == 1

            # Nobody reports back: the next read reaps the pass.
            clock.now_ms = (int(meta["worker_deadline_at"])
                            + core.STALE_WORKER_GRACE_MS + 1)
            status, body = senv.invoke(
                "GET", "/synthetic/sessions/{id}", user, session_id)
            assert status == 200, body
            assert body["session"]["status"] == "failed"
            reason = body["session"]["last_failure"]["reason"]
            assert "pass 1" in reason and "35" in reason and "50" in reason
            stored = _meta(senv, session_id)
            assert stored["status"] == "failed"
            assert stored["last_failure"]["reason"] == reason
            status, again = senv.invoke(
                "GET", "/synthetic/sessions/{id}", user, session_id)
            assert status == 200
            assert again["session"] == body["session"]
            assert len(_previews(senv, session_id)) == 35

            # Regenerate starts a new pass.
            status, body = senv.invoke(
                "POST", "/synthetic/sessions/{id}/generate", user,
                session_id, {})
            assert status == 202, body
            assert body["generation_pass"] == 2
            meta = _meta(senv, session_id)
            assert meta["status"] == "generating"
            assert "last_failure" not in meta
            assert len(payloads) == 2 and payloads[1]["generation_pass"] == 2
        finally:
            for restore in restores:
                restore()

    def test_duplicate_delivery_of_a_finished_worker_event_is_rejected(
            self, senv, actor):
        """Lambda may deliver an asynchronous event more than once even
        without an error: the second delivery of a finished pass's event
        runs nothing."""
        usecase_id, user = actor
        payloads = []
        restore = _patched(senv, "_invoke_worker_async", payloads.append)
        try:
            session_id = _start_pass(senv, user, usecase_id, sources=1,
                                     variations=2)
            clock = _Clock()
            result = _run_worker(senv, payloads[0], _TimedBedrockRuntime(clock))
            assert result["status"] == "done", result
            assert _meta(senv, session_id)["status"] == "awaiting_review"
            assert len(_previews(senv, session_id)) == 2

            result = _run_worker(senv, payloads[0], _TimedBedrockRuntime(clock))
            assert result["status"] == "stale", result
            assert len(_previews(senv, session_id)) == 2
            assert _meta(senv, session_id)["status"] == "awaiting_review"
            assert len(payloads) == 1
        finally:
            restore()

    def test_invocation_without_time_for_one_task_fails_the_pass_instead_of_chaining(
            self, senv, actor):
        """An invocation that cannot start a single task must not hand over
        (an endless chain of empty invocations): the pass fails with the
        reason."""
        usecase_id, user = actor
        clock = _Clock()
        payloads = []
        restores = [_patched(senv, "_now_ms", clock),
                    _patched(senv, "_invoke_worker_async", payloads.append)]
        try:
            session_id = _start_pass(senv, user, usecase_id)
            context = _LambdaContext(clock, 60_000)
            result = _run_worker(senv, payloads[0],
                                 _TimedBedrockRuntime(clock, context), context)
            assert result["status"] == "failed", result
            assert result["completed"] == 0 and result["failed"] == 0
            assert _previews(senv, session_id) == []
            meta = _meta(senv, session_id)
            assert meta["status"] == "failed"
            assert "time limit" in meta["last_failure"]["reason"]
            assert "pass 1" in meta["last_failure"]["reason"]
            assert len(payloads) == 1
        finally:
            for restore in restores:
                restore()

    def test_failed_hand_over_marks_the_pass_failed(self, senv, actor, core):
        """lambda:Invoke refusing the continuation leaves the written
        previews and moves the pass to `failed` naming the continuation,
        rather than leaving `generating` with nobody running."""
        usecase_id, user = actor
        clock = _Clock()
        payloads = []

        def capture_then_refuse(payload):
            if payloads:
                raise ClientError(
                    {"Error": {"Code": "TooManyRequestsException",
                               "Message": "rate"}}, "Invoke")
            payloads.append(payload)

        restores = [_patched(senv, "_now_ms", clock),
                    _patched(senv, "_invoke_worker_async", capture_then_refuse)]
        try:
            session_id = _start_pass(senv, user, usecase_id)
            context = _LambdaContext(clock, 900_000)
            result = _run_worker(senv, payloads[0],
                                 _TimedBedrockRuntime(clock, context), context)
            assert result["status"] == "failed", result
            expected_first = _per_invocation(core, 900_000, 50)
            assert result["completed"] == expected_first
            assert len(_previews(senv, session_id)) == expected_first
            meta = _meta(senv, session_id)
            assert meta["status"] == "failed"
            reason = meta["last_failure"]["reason"]
            assert "continuation" in reason
            assert f"{expected_first} of 50" in reason
            assert "TooManyRequestsException" in reason
            assert len(payloads) == 1
        finally:
            for restore in restores:
                restore()


@settings(deadline=None)
@given(
    sources=st.integers(min_value=1, max_value=4),
    variation_count=st.integers(min_value=1, max_value=4),
    stop_after=st.integers(min_value=0, max_value=16),
    failing=st.lists(st.booleans(), min_size=16, max_size=16),
)
def test_execute_generation_tasks_stops_before_the_first_refused_task(
        senv, core, sources, variation_count, stop_after, failing):
    """With ``should_stop`` answering True from its (k+1)-th call, the loop
    runs exactly ``tasks[:k]`` in order (completed and failed together),
    ``on_result`` saw those k previews, each carrying its task index, and
    the untouched tail is the caller's to hand over."""
    plan = _plan(core, [f"datasets/s{i}.png" for i in range(sources)],
                 variation_count)
    k = min(stop_after, len(plan))
    calls = [0]

    def should_stop():
        calls[0] += 1
        return calls[0] > k

    def invoke_task(task):
        if failing[task["task_index"]]:
            raise RuntimeError(f"boom {task['task_index']}")
        return {"staging_key": f"s/{task['task_index']}.png"}

    seen = []
    completed, failed = senv.synthetic_data.execute_generation_tasks(
        plan, invoke_task, seen.append, should_stop)
    ran = sorted(completed + failed, key=lambda p: p["task_index"])
    assert [p["task_index"] for p in ran] == [t["task_index"]
                                               for t in plan[:k]]
    assert [p["task_index"] for p in seen] == [t["task_index"]
                                               for t in plan[:k]]
    assert all("task_index" in p for p in seen)
    assert {p["task_index"] for p in completed} == {
        t["task_index"] for t in plan[:k] if not failing[t["task_index"]]}
    assert {p["task_index"] for p in failed} == {
        t["task_index"] for t in plan[:k] if failing[t["task_index"]]}


# ============================================================= routes

def _seed_generating_session(senv, usecase_id, core, clock, generation_pass,
                             sources=10, variations=5, **meta_overrides):
    """A `generating` session with a persisted ``sources`` x ``variations``
    plan for ``generation_pass`` and uploaded sources."""
    session_id = str(uuid.uuid4())
    source_images = _sources_for(senv, session_id, sources)
    plan = core.build_generation_plan(
        {"generation_model_id": "amazon.nova-canvas-v1:0"}, source_images,
        variations, "A metal casting with a scratch", {})
    overrides = {
        "status": "generating",
        "generation_pass": generation_pass,
        "generation_plan": plan,
        "generation_model_id": "amazon.nova-canvas-v1:0",
        "source_class": "defect",
        "source_images": source_images,
        "generation_params": {"variation_count": variations},
        "updated_at": clock.now_ms,
    }
    overrides.update(meta_overrides)
    senv.put_session_meta(usecase_id, session_id=session_id, **overrides)
    return session_id, plan


class TestGenerateAndReaper:
    def test_generate_refuses_a_running_pass_and_accepts_a_stale_one(
            self, senv, actor, core):
        usecase_id, user = actor
        clock = _Clock()
        payloads = []
        restores = [_patched(senv, "_now_ms", clock),
                    _patched(senv, "_invoke_worker_async", payloads.append)]
        try:
            session_id, plan = _seed_generating_session(
                senv, usecase_id, core, clock, generation_pass=1,
                worker_token="live-token",
                worker_deadline_at=clock.now_ms + 600_000)
            for task in plan[:7]:
                senv.put_preview(
                    session_id, generation_pass=1,
                    task_index=task["task_index"],
                    source_image_key=task["source_image"]["key"],
                    variation_index=task["variation_index"])
            before = _meta(senv, session_id)

            status, body = senv.invoke(
                "POST", "/synthetic/sessions/{id}/generate", user,
                session_id, {})
            assert status == 409, body
            assert "already running" in body["error"]
            assert "7 of 50" in body["error"]
            assert body["generation_pass"] == 1
            assert _meta(senv, session_id) == before
            assert payloads == []
            status, body = senv.invoke(
                "GET", "/synthetic/sessions/{id}", user, session_id)
            assert status == 200 and body["session"]["status"] == "generating"

            # The worker's deadline plus grace has passed: a dead pass.
            senv.sessions_table.update_item(
                Key={"session_id": session_id, "sk": "META"},
                UpdateExpression="SET worker_deadline_at = :d",
                ExpressionAttributeValues={
                    ":d": clock.now_ms - core.STALE_WORKER_GRACE_MS - 1})
            status, body = senv.invoke(
                "POST", "/synthetic/sessions/{id}/generate", user,
                session_id, {})
            assert status == 202, body
            assert body["generation_pass"] == 2
            meta = _meta(senv, session_id)
            assert meta["status"] == "generating"
            assert meta["generation_pass"] == 2
            assert meta["worker_token"] not in ("live-token", None, "")
            assert meta["worker_deadline_at"] == \
                clock.now_ms + senv.synthetic_data.WORKER_TIMEOUT_MS
            assert len(payloads) == 1
            assert payloads[0]["worker_token"] == meta["worker_token"]
            assert payloads[0]["generation_pass"] == 2
        finally:
            for restore in restores:
                restore()

    def test_stuck_session_shape_is_reaped_then_regenerates_to_completion(
            self, senv, actor, core):
        """Session 6115b94c as it is stored today: `generating`, pass 2,
        `updated_at` hours old, no worker fields, 130 failed previews of
        pass 2 (43 + 42 + 45, same tasks) without task indexes. Opening it
        moves it to `failed`; Regenerate runs pass 3 across two
        invocations to `awaiting_review` with exactly 50 pass-3 previews."""
        usecase_id, user = actor
        clock = _Clock()
        payloads = []
        restores = [_patched(senv, "_now_ms", clock),
                    _patched(senv, "_invoke_worker_async", payloads.append)]
        try:
            session_id, plan = _seed_generating_session(
                senv, usecase_id, core, clock, generation_pass=2,
                generation_params={"variation_count": 5,
                                   "oversize_handling": "resize_restore"},
                updated_at=clock.now_ms - 4 * HOUR_MS,
                last_failure={"reason": "ValidationException: Response "
                                        "payload size 20276590 bytes "
                                        "exceeds the maximum allowed size",
                              "at": clock.now_ms - 4 * HOUR_MS})
            for run_tasks in (43, 42, 45):
                for task in plan[:run_tasks]:
                    senv.put_preview(
                        session_id, generation_pass=2, status="failed",
                        failure_reason="ValidationException: Response "
                                       "payload size exceeds the maximum",
                        source_image_key=task["source_image"]["key"],
                        variation_index=task["variation_index"],
                        seed=task["seed"])
            assert len(_previews(senv, session_id)) == 130
            assert all("task_index" not in p
                       for p in _previews(senv, session_id))

            status, body = senv.invoke(
                "GET", "/synthetic/sessions/{id}", user, session_id)
            assert status == 200, body
            assert body["session"]["status"] == "failed"
            reason = body["session"]["last_failure"]["reason"]
            assert "pass 2" in reason and "130" in reason and "50" in reason
            assert len(body["previews"]) == 130
            assert _meta(senv, session_id)["status"] == "failed"
            status, again = senv.invoke(
                "GET", "/synthetic/sessions/{id}", user, session_id)
            assert status == 200 and again["session"] == body["session"]
            assert len(_previews(senv, session_id)) == 130

            status, body = senv.invoke(
                "POST", "/synthetic/sessions/{id}/generate", user,
                session_id, {})
            assert status == 202, body
            assert body["generation_pass"] == 3
            assert body["task_count"] == 50
            assert len(payloads) == 1

            context = _LambdaContext(clock, 900_000)
            result = _run_worker(senv, payloads[0],
                                 _TimedBedrockRuntime(clock, context), context)
            assert result["status"] == "continued", result
            assert len(payloads) == 2
            context = _LambdaContext(clock, 900_000)
            result = _run_worker(senv, payloads[1],
                                 _TimedBedrockRuntime(clock, context), context)
            assert result["status"] == "done", result

            meta = _meta(senv, session_id)
            assert meta["status"] == "awaiting_review"
            assert meta["generation_pass"] == 3
            previews = _previews(senv, session_id)
            assert len(previews) == 180
            pass_three = [p for p in previews if p["generation_pass"] == 3]
            assert len(pass_three) == 50
            assert all(p["status"] == "completed" for p in pass_three)
            by_index, by_key = _distinct_pairs(pass_three)
            assert len(by_index) == 50 and len(by_key) == 50
            assert len(payloads) == 2
        finally:
            for restore in restores:
                restore()

    def test_reaper_leaves_live_and_non_generating_sessions_alone(
            self, senv, actor, core):
        usecase_id, user = actor
        clock = _Clock()
        restore = _patched(senv, "_now_ms", clock)
        try:
            reviewed, _ = _seed_generating_session(
                senv, usecase_id, core, clock, generation_pass=1,
                status="awaiting_review",
                updated_at=clock.now_ms - 48 * HOUR_MS)
            before = _meta(senv, reviewed)
            status, body = senv.invoke(
                "GET", "/synthetic/sessions/{id}", user, reviewed)
            assert status == 200
            assert body["session"]["status"] == "awaiting_review"
            assert _meta(senv, reviewed) == before

            live, _ = _seed_generating_session(
                senv, usecase_id, core, clock, generation_pass=1,
                worker_token="live-token",
                worker_deadline_at=clock.now_ms + 600_000,
                updated_at=clock.now_ms - 48 * HOUR_MS)
            before = _meta(senv, live)
            status, body = senv.invoke(
                "GET", "/synthetic/sessions/{id}", user, live)
            assert status == 200
            assert body["session"]["status"] == "generating"
            assert _meta(senv, live) == before
        finally:
            restore()
