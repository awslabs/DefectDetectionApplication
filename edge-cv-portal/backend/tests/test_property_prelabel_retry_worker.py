"""
retry_prelabels worker property tests.

Spec: grounded-sam-prompt-guardrails-and-prelabel-retry, task 2.2.

Four properties over the real `retry_prelabels_job` in
dda_labeling_worker.py, driven through the worker `handler` with
{action: 'retry_prelabels', job_id} against the moto-backed stack from
conftest.py (the test_dda_labeling_worker_distribute.py import
scaffolding), 100 Hypothesis examples each:

**Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
Property 6: The reset flips exactly the Failed tasks to Pending with
errors removed; every other task byte-identical**
**Validates: Requirements 6.1, 6.4**

**Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
Property 7: The enqueued set equals the reset set, each message the
distributor's exact shape for its family**
**Validates: Requirements 5.10, 6.2**

**Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
Property 8: Skip-verification counters re-arm by exactly the reset
count with review_ready cleared; other jobs' counters untouched**
**Validates: Requirements 6.3**

**Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
Property 9: Repeating the retry enqueues at most one message per
originally-Failed task**
**Validates: Requirements 6.5**

Oracles
-------
Restated in this file, never imported from the code under test:

- **Reset transition** (design Data Models "Task item transition"):
  a task whose stored `prelabel_status` is `Failed` moves to
  `prelabel_status = 'Pending'` with `prelabel_error` and
  `autolabel_error` both REMOVEd and `updated_at` bumped; every other
  attribute keeps its stored value. A task whose `prelabel_status` is
  `Available`, `Pending`, `None` (the distribute-time string), or
  absent stays attribute-for-attribute identical.
- **Fanout_Message** (design Data Models, byte-identical to the
  distributor's): `{job_id, task_id, image_s3_uri (the task item's
  stored value), modality (the job's task_type), label_set, model}`
  plus `detection_prompt` for the `llm:` family (the stored
  auto_label.detection_prompt, '' when absent/empty) and
  `per_label_prompts` for Skip_Verification_Mode jobs. The model is
  resolved with the distributor's precedence: an `llm:` value wins
  outright; otherwise a skip-verification job sends
  `bedrock:{bedrock_model_id}` (the job field, not the auto_label
  value); every other job sends the stored auto_label.model.
- **Counter re-arm** (design Data Models "Skip-verification re-arm"):
  with reset count n > 0 a skip-verification job's `autolabel_pending`
  grows by exactly n, `autolabel_completed_count` shrinks by exactly
  n, and `review_ready` becomes False; with n = 0 the job record is
  untouched. A non-skip-verification job's record never changes and
  never gains any of the three attributes.
- **Idempotence** (Req 6.5's conditional-reset model): a second run
  finds zero Failed tasks, so it resets nothing, enqueues nothing, and
  moves no counters — across both runs each originally-Failed task
  receives exactly one message.

Harness: the module-scoped `worker` fixture imports the real module
inside the moto mock (sys.modules popped first, the distribute-suite
convention) and replaces its module-level `sqs_client` with a fake
capturing every send_message_batch call (AUTOLABEL_QUEUE_URL set so
the fan-out engages). Jobs and task populations are seeded straight
into the moto tables — the properties quantify over record states
(family x skip-verification, mixed prelabel_status values, stray
error and extra attributes, derived-consistent counters) that no
single API path constructs. Every job is seeded Retry_Eligible
(eligibility itself is Property 4, another file's concern). Hypothesis
cannot consume function-scoped fixtures, so per-example isolation
comes from uuid job ids.
"""
import json
import os
import sys
import uuid
from collections import Counter
from datetime import datetime
from types import SimpleNamespace

import pytest
from boto3.dynamodb.conditions import Key
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

QUEUE_URL = ("https://sqs.us-east-1.amazonaws.com/123456789012/"
             "dda-portal-autolabel-queue")

_MODALITIES = ("Classification", "Segmentation", "ObjectDetection")
_BEDROCK_IDS = ("anthropic.claude-3-haiku", "us.amazon.nova-lite-v1:0")
_LLM_MODELS = ("llm:us.amazon.nova-pro-v1:0",
               "llm:us.anthropic.claude-sonnet-4-20250514-v1:0")
# The reset transition's moving parts; everything else on a task item
# must survive the retry byte-identical.
_RESET_TOUCHED = ("prelabel_status", "prelabel_error", "autolabel_error",
                  "updated_at")
# The counter re-arm's moving parts on a skip-verification job record.
_REARM_TOUCHED = ("autolabel_pending", "autolabel_completed_count",
                  "review_ready", "updated_at")


# --------------------------------------------------------- fake SQS client

class FakeSqsClient:
    """Captures the worker's send_message_batch fan-out. No moto queue:
    the properties assert on the exact entries the code sends. Every
    entry succeeds, mirroring a healthy queue."""

    def __init__(self):
        self.calls = []  # [(queue_url, [entry, ...])]

    def send_message_batch(self, QueueUrl=None, Entries=None):
        entries = list(Entries or [])
        self.calls.append((QueueUrl, entries))
        return {"Successful": [{"Id": entry["Id"]} for entry in entries],
                "Failed": []}

    def reset(self):
        self.calls.clear()

    def bodies(self):
        """Every enqueued message body across all captured batches."""
        return [json.loads(entry["MessageBody"])
                for _, entries in self.calls for entry in entries]


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def worker(aws_stack):
    """The real dda_labeling_worker module imported inside the moto
    mock (the test_dda_labeling_worker_distribute.py convention:
    sys.modules popped so module-level boto3 resources bind to moto),
    its module-level sqs_client replaced by the capturing fake, and
    AUTOLABEL_QUEUE_URL set so the fan-out engages."""
    sys.modules.pop("dda_labeling", None)
    sys.modules.pop("dda_labeling_worker", None)
    import dda_labeling_worker

    fake_sqs = FakeSqsClient()
    dda_labeling_worker.sqs_client = fake_sqs

    previous = os.environ.get("AUTOLABEL_QUEUE_URL")
    os.environ["AUTOLABEL_QUEUE_URL"] = QUEUE_URL
    yield SimpleNamespace(module=dda_labeling_worker, sqs=fake_sqs,
                          tables=aws_stack.tables)
    if previous is None:
        os.environ.pop("AUTOLABEL_QUEUE_URL", None)
    else:
        os.environ["AUTOLABEL_QUEUE_URL"] = previous


# -------------------------------------------------------------- generators

# Printable unicode without surrogates — the sibling property suites'
# alphabet (periods legal: the worker never judges prompt content).
_TEXT_ALPHABET = st.characters(min_codepoint=32, max_codepoint=0x2FFF,
                               blacklist_categories=("Cs",))
_text = st.text(alphabet=_TEXT_ALPHABET, min_size=1,
                max_size=24).filter(lambda t: t.strip())
_labels = st.text(alphabet=_TEXT_ALPHABET, min_size=1,
                  max_size=12).map(str.strip).filter(bool)

# prelabel_error / autolabel_error values: the incident's error string
# plus arbitrary text (duplicates across tasks are fine).
_error_texts = st.one_of(
    st.just('Grounded-SAM worker failed: {"errorMessage": "caption token '
            'spans (2) do not align with the 1 prompts; a prompt likely '
            'contains inner sentence punctuation", "errorType": '
            '"ValueError"}'),
    _text,
)

# Arbitrary extra task attributes the reset must carry through
# untouched. Prefixed so they can never collide with the attributes
# under test or the seeded realistic fields.
_extra_names = st.text(alphabet="abcdefghijklmnop_", min_size=1,
                       max_size=10).map(lambda name: f"x_{name}")
_extra_values = st.one_of(_text, st.integers(min_value=0, max_value=10**6),
                          st.booleans())

# detection_prompt states for llm: jobs: absent (None), stored-empty,
# the fixture prompt, arbitrary text — the message carries the stored
# value or '' when absent/empty.
_detection_prompts = st.one_of(
    st.none(),
    st.just(""),
    st.just("Find every defect. Mark each one."),
    _text,
)


@st.composite
def _task_specs(draw):
    """One task item state: prelabel_status drawn across Failed /
    Available / Pending / 'None' (the distribute-time string) / absent,
    error attributes independently present or absent (stale errors on
    non-Failed tasks included — the reset must leave them alone), and
    arbitrary extra attributes."""
    return SimpleNamespace(
        status=draw(st.sampled_from(
            ("Failed", "Available", "Pending", "None", None))),
        prelabel_error=draw(st.one_of(st.none(), _error_texts)),
        autolabel_error=draw(st.one_of(st.none(), _error_texts)),
        extras=draw(st.dictionaries(_extra_names, _extra_values,
                                    max_size=3)),
    )


@st.composite
def _retry_cases(draw):
    """One Retry_Eligible job (family x skip-verification, counters
    derived consistent) plus a task population — the Property 6-9
    input space. 'hardwire' is the legacy skip-verification shape with
    no auto_label document at all (model resolves from
    bedrock_model_id alone)."""
    skip_verification = draw(st.booleans())
    families = ("grounded-sam", "sam", "bedrock", "llm")
    family = draw(st.sampled_from(families + ("hardwire",))
                  if skip_verification else st.sampled_from(families))

    if family == "grounded-sam":
        model = "grounded-sam"
    elif family == "sam":
        model = "sam"
    elif family == "bedrock":
        model = f"bedrock:{draw(st.sampled_from(_BEDROCK_IDS))}"
    elif family == "llm":
        model = draw(st.sampled_from(_LLM_MODELS))
    else:  # hardwire — skip-verification job without auto_label
        model = None

    prompt_overrides = None
    if family == "grounded-sam" and draw(st.booleans()):
        # Prompt_Overrides ride the job record, not the fan-out
        # message — planted so a leak into a message body fails the
        # deep-equal oracle.
        prompt_overrides = draw(st.dictionaries(_labels, _text,
                                                min_size=1, max_size=2))

    specs = draw(st.lists(_task_specs(), min_size=0, max_size=7))
    if specs and draw(st.booleans()):
        # Keep the Failed->Pending transition well-weighted without
        # excluding the legitimate zero-Failed edge.
        specs[draw(st.integers(
            min_value=0, max_value=len(specs) - 1))].status = "Failed"

    return SimpleNamespace(
        family=family,
        model=model,
        skip_verification=skip_verification,
        # Independently drawn, so a skip-verification job of the
        # bedrock family can carry a bedrock_model_id differing from
        # its auto_label model id — the resolution must pick the job
        # field (design: the distributor's precedence).
        bedrock_model_id=(draw(st.sampled_from(_BEDROCK_IDS))
                          if skip_verification else None),
        per_label_prompts=(draw(st.dictionaries(_labels, _text,
                                                min_size=1, max_size=2))
                           if skip_verification else None),
        detection_prompt=(draw(_detection_prompts)
                          if family == "llm" else None),
        prompt_overrides=prompt_overrides,
        task_type=draw(st.sampled_from(_MODALITIES)),
        label_set=draw(st.lists(_labels, min_size=1, max_size=3,
                                unique=True)),
        tasks=specs,
    )


# ------------------------------------------------------- seeding / storage

def _seed_case(tables, case):
    """Seed the job record and its task population into the moto
    tables. Skip-verification counters are seeded in the consistent
    state the shipped machinery maintains: autolabel_pending counts
    the Pending tasks, autolabel_completed_count the resolved
    (Available + Failed) tasks, review_ready true iff nothing pends.
    Returns (job_id, the seeded job dict, {task_id: seeded item})."""
    job_id = f"labeling-{uuid.uuid4().hex[:12]}"
    seeded_at = 1_000
    job = {
        "job_id": job_id,
        "usecase_id": f"uc-{job_id}",
        "job_name": f"retry-{job_id}",
        "labeling_backend": "DDA",
        "status": "InProgress",
        "task_type": case.task_type,
        "label_set": list(case.label_set),
        "image_count": len(case.tasks),
        "skip_verification": case.skip_verification,
        "created_at": seeded_at,
        "updated_at": seeded_at,
    }
    if case.model is not None:
        auto_label = {"enabled": True, "model": case.model}
        if case.family == "llm" and case.detection_prompt is not None:
            auto_label["detection_prompt"] = case.detection_prompt
        if case.prompt_overrides:
            auto_label["prompt_overrides"] = dict(case.prompt_overrides)
        job["auto_label"] = auto_label
    if case.skip_verification:
        pending = sum(1 for spec in case.tasks if spec.status == "Pending")
        resolved = sum(1 for spec in case.tasks
                       if spec.status in ("Available", "Failed"))
        job["bedrock_model_id"] = case.bedrock_model_id
        job["per_label_prompts"] = dict(case.per_label_prompts)
        job["autolabel_pending"] = pending
        job["autolabel_completed_count"] = resolved
        job["review_ready"] = pending == 0
    tables.labeling_jobs.put_item(Item=job)

    seeded = {}
    for index, spec in enumerate(case.tasks):
        task_id = f"task-{index:06d}"
        item = {
            "job_id": job_id,
            "task_id": task_id,
            "image_s3_uri": f"s3://retry-test-bucket/{job_id}/"
                            f"img-{index:03d}.jpg",
            "image_key": f"{job_id}/img-{index:03d}.jpg",
            "usecase_id": job["usecase_id"],
            "assignee_user_id": ("AUTO" if case.skip_verification
                                 else f"user-{index}"),
            "status": "Assigned",
            "created_at": seeded_at,
            "updated_at": seeded_at,
        }
        if spec.status is not None:
            item["prelabel_status"] = spec.status
        if spec.prelabel_error is not None:
            item["prelabel_error"] = spec.prelabel_error
        if spec.autolabel_error is not None:
            item["autolabel_error"] = spec.autolabel_error
        item.update(spec.extras)
        tables.labeling_tasks.put_item(Item=item)
        seeded[task_id] = item
    return job_id, job, seeded


def _stored_tasks(tables, job_id):
    """Every stored task item of the job, keyed by task_id (<= 7
    items, single page)."""
    response = tables.labeling_tasks.query(
        KeyConditionExpression=Key("job_id").eq(job_id))
    return {item["task_id"]: item for item in response.get("Items", [])}


def _stored_job(tables, job_id):
    return tables.labeling_jobs.get_item(
        Key={"job_id": job_id}).get("Item")


def _run_retry(worker, job_id):
    """Drive the real retry_prelabels_job through the worker's action
    dispatcher and fail loudly on an error/skip result — every seeded
    job is Retry_Eligible."""
    result = worker.module.handler(
        {"action": "retry_prelabels", "job_id": job_id}, None)
    assert "error" not in result, result
    assert not result.get("skipped"), result
    return result


def _failed_task_ids(items):
    """The originally-Failed set: tasks whose stored prelabel_status
    is the string 'Failed'."""
    return {task_id for task_id, item in items.items()
            if item.get("prelabel_status") == "Failed"}


# ----------------------------------------------------------------- oracles

def _expected_model(job):
    """The distributor's model resolution restated (design Data
    Models): an llm: value takes precedence outright; otherwise a
    skip-verification job sends bedrock:{bedrock_model_id} (the job
    field, whatever the auto_label value says); every other job sends
    the stored auto_label.model."""
    model = (job.get("auto_label") or {}).get("model")
    if isinstance(model, str) and model.startswith("llm:"):
        return model
    if job.get("skip_verification"):
        return f"bedrock:{job['bedrock_model_id']}"
    return model


def _expected_fanout_message(job, task_id, image_s3_uri):
    """The design's Fanout_Message oracle restated: {job_id, task_id,
    image_s3_uri (from the task item), modality (the job's task_type),
    label_set, model} + detection_prompt for the llm: family (stored
    value, '' when absent/empty) + per_label_prompts for
    skip-verification jobs. Exactly these keys — anything extra (a
    leaked prompt_overrides, say) fails the deep-equal."""
    model = _expected_model(job)
    message = {
        "job_id": job["job_id"],
        "task_id": task_id,
        "image_s3_uri": image_s3_uri,
        "modality": job["task_type"],
        "label_set": list(job["label_set"]),
        "model": model,
    }
    if isinstance(model, str) and model.startswith("llm:"):
        message["detection_prompt"] = (
            (job.get("auto_label") or {}).get("detection_prompt") or "")
    if job.get("skip_verification"):
        message["per_label_prompts"] = dict(job["per_label_prompts"])
    return message


# =========================================================================== #
# Property 6: The reset flips exactly the Failed tasks to Pending with
# errors removed; every other task byte-identical
# =========================================================================== #

class TestProperty6ResetFlipsExactlyTheFailedTasks:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_retry_cases())
    def test_property_reset_flips_exactly_the_failed_tasks(
            self, worker, case):
        """Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
        Property 6: The reset flips exactly the Failed tasks to Pending
        with errors removed; every other task byte-identical — *For
        any* task population with arbitrary `prelabel_status` values
        and error attributes, running the Retry_Action SHALL set
        exactly the Failed tasks' `prelabel_status` to `Pending` with
        `prelabel_error` and `autolabel_error` removed, and SHALL leave
        every Available, Pending, None, and Inactive task item
        attribute-for-attribute unchanged.

        **Validates: Requirements 6.1, 6.4**
        """
        worker.sqs.reset()
        job_id, _, _ = _seed_case(worker.tables, case)
        before = _stored_tasks(worker.tables, job_id)
        failed_ids = _failed_task_ids(before)
        floor = int(datetime.utcnow().timestamp())

        result = _run_retry(worker, job_id)

        assert result["reset_count"] == len(failed_ids), result
        after = _stored_tasks(worker.tables, job_id)
        assert set(after) == set(before), (
            "the retry created or deleted task items")

        for task_id, item_before in before.items():
            item_after = after[task_id]
            if task_id in failed_ids:
                # Failed -> Pending, both error attributes REMOVEd,
                # updated_at bumped...
                assert item_after.get("prelabel_status") == "Pending", (
                    f"{task_id}: Failed task not reset to Pending: "
                    f"{item_after.get('prelabel_status')!r}")
                assert "prelabel_error" not in item_after, (
                    f"{task_id}: prelabel_error survived the reset")
                assert "autolabel_error" not in item_after, (
                    f"{task_id}: autolabel_error survived the reset")
                assert int(item_after["updated_at"]) >= floor, (
                    f"{task_id}: updated_at not bumped: "
                    f"{item_after['updated_at']}")
                # ...and every other attribute preserved exactly.
                expected = {key: value
                            for key, value in item_before.items()
                            if key not in ("prelabel_error",
                                           "autolabel_error")}
                expected["prelabel_status"] = "Pending"
                expected["updated_at"] = item_after["updated_at"]
                assert item_after == expected, (
                    f"{task_id}: reset disturbed unrelated attributes: "
                    f"{item_after!r} != {expected!r}")
            else:
                # Non-Failed (Available / Pending / 'None' / absent):
                # attribute-for-attribute identical, stale errors and
                # extras included.
                assert item_after == item_before, (
                    f"{task_id}: non-Failed task "
                    f"(prelabel_status="
                    f"{item_before.get('prelabel_status')!r}) modified: "
                    f"{item_after!r} != {item_before!r}")


# =========================================================================== #
# Property 7: The enqueued set equals the reset set, each message the
# distributor's exact shape for its family
# =========================================================================== #

class TestProperty7EnqueuedSetEqualsResetSetInDistributorShape:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_retry_cases())
    def test_property_enqueued_set_equals_reset_set_in_distributor_shape(
            self, worker, case):
        """Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
        Property 7: The enqueued set equals the reset set, each message
        the distributor's exact shape for its family — *For any* job
        configuration (family drawn from grounded-sam/sam/bedrock:/
        llm:, with and without Skip_Verification_Mode) and *any* task
        population, the Retry_Action SHALL enqueue exactly one message
        per reset task, each message equal to the Fanout_Message the
        distributor would build for that task — same key set,
        `image_s3_uri` from the task item, the
        llm-over-skip-verification model precedence, `detection_prompt`
        for `llm:` and `per_label_prompts` for skip-verification jobs.

        **Validates: Requirements 5.10, 6.2**
        """
        worker.sqs.reset()
        job_id, job, _ = _seed_case(worker.tables, case)
        before = _stored_tasks(worker.tables, job_id)
        failed_ids = _failed_task_ids(before)

        result = _run_retry(worker, job_id)

        # Exactly one message per reset task (the reset set is the
        # whole originally-Failed set here: nothing races the
        # conditional updates).
        assert result["reset_count"] == len(failed_ids), result
        assert result["enqueued_count"] == len(failed_ids), result
        bodies = worker.sqs.bodies()
        assert len(bodies) == len(failed_ids), (
            f"expected one message per reset task: {bodies!r}")
        assert {body["task_id"] for body in bodies} == failed_ids, (
            f"enqueued set drifted from the reset set: {bodies!r}")

        # Each message deep-equals the distributor's Fanout_Message
        # oracle for its family, image_s3_uri from the stored task item.
        for body in bodies:
            item = before[body["task_id"]]
            expected = _expected_fanout_message(
                job, body["task_id"], item["image_s3_uri"])
            assert body == expected, (
                f"message shape drifted from the distributor's for "
                f"family={case.family!r} "
                f"skip_verification={case.skip_verification!r}: "
                f"{body!r} != {expected!r}")

        # The distributor's batching contract: <= 10 entries per
        # send_message_batch call.
        for _, entries in worker.sqs.calls:
            assert 1 <= len(entries) <= 10


# =========================================================================== #
# Property 8: Skip-verification counters re-arm by exactly the reset
# count with review_ready cleared; other jobs' counters untouched
# =========================================================================== #

class TestProperty8SkipVerificationCountersReArm:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_retry_cases())
    def test_property_skip_verification_counters_rearm_by_reset_count(
            self, worker, case):
        """Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
        Property 8: Skip-verification counters re-arm by exactly the
        reset count with review_ready cleared; other jobs' counters
        untouched — *For any* Skip_Verification_Mode job with counters
        in *any* consistent state and *any* Failed-task count n >= 0,
        the Retry_Action SHALL increase `autolabel_pending` by exactly
        the reset count, decrease `autolabel_completed_count` by
        exactly the reset count, and set `review_ready` false when the
        reset count is positive; and *for any* non-skip-verification
        job the Retry_Action SHALL write none of those attributes.

        **Validates: Requirements 6.3**
        """
        worker.sqs.reset()
        job_id, _, _ = _seed_case(worker.tables, case)
        job_before = _stored_job(worker.tables, job_id)
        failed_count = len(
            _failed_task_ids(_stored_tasks(worker.tables, job_id)))
        floor = int(datetime.utcnow().timestamp())

        result = _run_retry(worker, job_id)

        assert result["reset_count"] == failed_count, result
        job_after = _stored_job(worker.tables, job_id)

        if case.skip_verification and failed_count > 0:
            # pending += n, completed -= n, review_ready cleared,
            # updated_at bumped...
            assert (int(job_after["autolabel_pending"])
                    == int(job_before["autolabel_pending"])
                    + failed_count), (
                f"autolabel_pending not re-armed by the reset count: "
                f"{job_before['autolabel_pending']} -> "
                f"{job_after['autolabel_pending']} (n={failed_count})")
            assert (int(job_after["autolabel_completed_count"])
                    == int(job_before["autolabel_completed_count"])
                    - failed_count), (
                f"autolabel_completed_count not decreased by the reset "
                f"count: {job_before['autolabel_completed_count']} -> "
                f"{job_after['autolabel_completed_count']} "
                f"(n={failed_count})")
            assert job_after["review_ready"] is False, (
                f"review_ready not cleared: "
                f"{job_after['review_ready']!r}")
            assert int(job_after["updated_at"]) >= floor
            # ...and every other job attribute untouched.
            untouched_after = {key: value
                               for key, value in job_after.items()
                               if key not in _REARM_TOUCHED}
            untouched_before = {key: value
                                for key, value in job_before.items()
                                if key not in _REARM_TOUCHED}
            assert untouched_after == untouched_before, (
                f"the re-arm disturbed unrelated job attributes: "
                f"{untouched_after!r} != {untouched_before!r}")
        else:
            # Zero resets (skip or not) and every non-skip job: the
            # job record is byte-identical.
            assert job_after == job_before, (
                f"job record modified "
                f"(skip_verification={case.skip_verification!r}, "
                f"n={failed_count}): {job_after!r} != {job_before!r}")
            if not case.skip_verification:
                # Non-skip jobs never gain the counter attributes.
                for attribute in ("autolabel_pending",
                                  "autolabel_completed_count",
                                  "review_ready"):
                    assert attribute not in job_after, (
                        f"non-skip-verification job gained "
                        f"{attribute!r}")


# =========================================================================== #
# Property 9: Repeating the retry enqueues at most one message per
# originally-Failed task
# =========================================================================== #

class TestProperty9RepeatedRetryEnqueuesAtMostOncePerFailedTask:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_retry_cases())
    def test_property_repeated_retry_enqueues_at_most_once_per_task(
            self, worker, case):
        """Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
        Property 9: Repeating the retry enqueues at most one message
        per originally-Failed task — *For any* task population, running
        the Retry_Action twice in succession (the conditional-reset
        model of two concurrent retries) SHALL enqueue exactly one
        message total per originally-Failed task, the second run
        resetting zero tasks and moving no counters.

        **Validates: Requirements 6.5**
        """
        worker.sqs.reset()
        job_id, _, _ = _seed_case(worker.tables, case)
        failed_ids = _failed_task_ids(_stored_tasks(worker.tables, job_id))

        first = _run_retry(worker, job_id)
        tasks_between = _stored_tasks(worker.tables, job_id)
        job_between = _stored_job(worker.tables, job_id)

        second = _run_retry(worker, job_id)

        # The second run resets nothing and enqueues nothing...
        assert first["reset_count"] == len(failed_ids), first
        assert second["reset_count"] == 0, second
        assert second["enqueued_count"] == 0, second
        # ...moves no counters and touches no task.
        assert _stored_job(worker.tables, job_id) == job_between, (
            "the second retry moved job counters")
        assert _stored_tasks(worker.tables, job_id) == tasks_between, (
            "the second retry modified task items")

        # Across both runs: exactly one message per originally-Failed
        # task, none for any other task.
        bodies = worker.sqs.bodies()
        counts = Counter(body["task_id"] for body in bodies)
        assert set(counts) == failed_ids, (
            f"messages drifted from the originally-Failed set: "
            f"{sorted(counts)!r} != {sorted(failed_ids)!r}")
        assert all(count == 1 for count in counts.values()), (
            f"a task received more than one message: {counts!r}")
        assert len(bodies) == len(failed_ids)
