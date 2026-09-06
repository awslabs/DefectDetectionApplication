"""
POST /labeling/{id}/rerun-prelabels route property tests.

Spec: grounded-sam-prompt-guardrails-and-prelabel-retry, task 1.3.

Two properties over the real `rerun_prelabels` route in dda_labeling.py
(driven through the module handler's router, so `_inject_job_usecase_scope`
and the real @rbac_check path run), against the moto-backed stack from
conftest.py — the test_dda_labeling_create_job.py module-scoped-fixture
scaffolding with a fake Lambda client installed at
`dda_labeling.lambda_client` capturing the `_invoke_labeling_worker`
payloads. 100 Hypothesis examples per property:

**Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
Property 4: Retry is accepted iff the job is Retry_Eligible, and
rejection mutates nothing** (route arm) — *For any* job record state
(backend, status, `review_finalized`, `auto_label.enabled`,
`skip_verification`, Failed-task count) and an authorized caller, the
retry SHALL proceed exactly when the job is a Retry_Eligible_Job; every
rejection SHALL name the unmet condition (404 for a missing job) and
SHALL leave the job record, every task item, the fake Lambda
invocations, and the audit trail's non-denial entries unchanged.
**Validates: Requirements 5.4, 6.8**

**Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
Property 5: Retry override validation equals creation's rules and
persists before triggering; rejection leaves record, tasks, and queue
untouched** — *For any* grounded-sam Retry_Request body
(`prompt_overrides` absent, or maps mixing valid, blank, period-bearing,
over-length, unknown-key, and non-string values) and *any* persisted
override state (including period-bearing persisted overrides and
period-bearing labels), the request SHALL be accepted exactly when
creation's override rules and the guardrail hold over the resulting
Effective_Prompts (submitted survivors, else persisted survivors, else
label names); acceptance SHALL persist the surviving submitted overrides
character-for-character (key removed when none survives) before the
worker invocation; rejection SHALL enumerate the offenses and change
nothing; and *for any* non-grounded-sam job a body carrying
`prompt_overrides` SHALL be rejected.
**Validates: Requirements 5.5, 5.6, 5.7, 5.8**

Oracles
-------
Restated in this file, never imported from the code under test:

- **Retry_Eligible** (requirements glossary): `labeling_backend == 'DDA'`
  AND `status == 'InProgress'` AND (`auto_label.enabled` OR
  `skip_verification`) AND no truthy `review_finalized` AND at least one
  task item with `prelabel_status == 'Failed'`. Each miss answers the
  distinct 400 of the design's error-handling table, checked in the
  design's documented gate order (non-DDA -> status -> autolabel-off ->
  review-finalized -> zero-failed); a missing job answers 404.
- **Override rules = creation's, verbatim** (Req 5.5): the submitted map
  replaces the persisted one — keys must belong to the job's Label_Set,
  values must be strings of raw length at most 256, blank-after-trim
  values are dropped silently, survivors are kept character-for-character.
  The Prompt_Guardrail (the design's Data Models oracle) is then judged
  over the resulting Effective_Prompts: submitted survivors when a body
  was sent, else the persisted map, else the label name; a label offends
  iff `'.'` is in its effective prompt, reported with its source
  (period-bearing surviving override vs period-bearing label name left as
  the fallback).
- **Persist-before-trigger** (Req 5.5): the fake Lambda client snapshots
  the job record at invoke time, so the test can prove the surviving
  overrides were already persisted when the Retry_Action was triggered.

Authorization: the caller holds MANAGE_LABELING_JOBS in the job's
Use_Case scope through the JWT custom:role claim (DataScientist), the
scaffolding's user seeding; skip-verification jobs use a UseCaseAdmin so
the Admin_Review-mirroring 403 gate (Req 5.3, an example-tested branch)
never interferes with the eligibility factors under test here.

Harness reuse (Hypothesis cannot consume function-scoped fixtures): the
module-scoped `dda` fixture follows test_dda_labeling_create_job.py;
per-example environments (fresh Use_Case + job + tasks, uuid-isolated)
are built inside the test bodies.
"""
import json
import os
import sys
import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest
from boto3.dynamodb.conditions import Key
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

REGION = "us-east-1"
DATASET_BUCKET = "test-retry-prop-dataset"  # name only; the route never reads S3
WORKER_FUNCTION_NAME = "test-dda-retry-prop-worker"

# The incident (job labeling-8022a9dc): an instruction-style override
# with an inner sentence period.
INCIDENT_PROMPT = (
    "draw and fill in the gaps between the broken cookie pieces within "
    "the bounds of the image. If there is a large crack, fill it in")
CORRECTED_PROMPT = "gap between broken cookie pieces"


class SnapshottingLambdaClient:
    """The test_dda_labeling_create_job.py FakeLambdaClient shape, plus a
    job-record snapshot taken at invoke time: reading the job while the
    (fake) worker is being triggered proves any override persistence
    happened BEFORE the trigger (Req 5.5's persist-before-trigger)."""

    def __init__(self, jobs_table):
        self.jobs_table = jobs_table
        self.invocations = []
        self.job_snapshots = []

    def invoke(self, **kwargs):
        payload = json.loads(kwargs.get("Payload") or "{}")
        job_id = payload.get("job_id")
        snapshot = (self.jobs_table.get_item(Key={"job_id": job_id})
                    .get("Item") if job_id else None)
        self.job_snapshots.append(snapshot)
        self.invocations.append(kwargs)
        return {"StatusCode": 202}


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock with
    the snapshotting fake Lambda client installed, and the worker
    function name wired so _invoke_labeling_worker reaches the fake
    (restored at module teardown). The rerun route never calls Cognito,
    so no fake Cognito client is needed."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling

    fake_lambda = SnapshottingLambdaClient(aws_stack.tables.labeling_jobs)
    dda_labeling.lambda_client = fake_lambda

    previous = os.environ.get("DDA_LABELING_WORKER_FUNCTION_NAME")
    os.environ["DDA_LABELING_WORKER_FUNCTION_NAME"] = WORKER_FUNCTION_NAME
    try:
        yield SimpleNamespace(module=dda_labeling, lambda_client=fake_lambda)
    finally:
        if previous is None:
            os.environ.pop("DDA_LABELING_WORKER_FUNCTION_NAME", None)
        else:
            os.environ["DDA_LABELING_WORKER_FUNCTION_NAME"] = previous


class RetryRouteEnv:
    """Per-example seeding + invocation facade (the RetryEnv shape from
    the route example suite, rebuilt fresh per Hypothesis example)."""

    def __init__(self, stack, dda):
        self.stack = stack
        self.dda = dda
        self.usecase_id = f"uc-{uuid.uuid4()}"
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Prelabel Retry Property Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })
        # DataScientist holds MANAGE_LABELING_JOBS in the job's scope via
        # the JWT custom:role fallback; UseCaseAdmin additionally passes
        # the skip-verification admin gate (Req 5.3).
        self.manager = self._make_user("DataScientist")
        self.admin = self._make_user("UseCaseAdmin")

    # ------------------------------------------------------------ setup
    @staticmethod
    def _make_user(role):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    def put_job(self, backend="DDA", status="InProgress", auto_label=None,
                skip_verification=False, review_finalized="absent",
                label_set=None, task_type="Segmentation"):
        job_id = f"labeling-{uuid.uuid4().hex[:10]}"
        item = {
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": f"job-{job_id}",
            "labeling_backend": backend,
            "status": status,
            "task_type": task_type,
            "label_set": list(label_set or ["scratch", "dent"]),
            "dataset_bucket": DATASET_BUCKET,
            "dataset_prefix": "datasets/x/",
            "image_count": 0,
            "created_at": 1,
            "created_by": self.manager["user_id"],
        }
        if auto_label is not None:
            item["auto_label"] = auto_label
        if skip_verification:
            item["skip_verification"] = True
        if review_finalized != "absent":
            item["review_finalized"] = review_finalized
        self.stack.tables.labeling_jobs.put_item(Item=item)
        return job_id

    def put_tasks(self, job_id, failed_count, extra_statuses):
        """`failed_count` Failed tasks plus one task per extra status
        ('Available' | 'Pending' | 'none' = attribute absent), the shape
        the distributor + consumer leave behind."""
        index = 0
        for index in range(failed_count):
            self._put_task(job_id, index, "Failed",
                           error=f"reason-{index % 2}")
        for offset, extra in enumerate(extra_statuses):
            self._put_task(job_id, failed_count + offset,
                           None if extra == "none" else extra)

    def _put_task(self, job_id, index, prelabel_status, error=None):
        image_key = f"datasets/x/img-{index:03d}.jpg"
        item = {
            "job_id": job_id,
            "task_id": f"task-{index:06d}",
            "image_s3_uri": f"s3://{DATASET_BUCKET}/{image_key}",
            "image_key": image_key,
            "usecase_id": self.usecase_id,
            "assignee_user_id": "AUTO",
            "status": "Assigned",
            "created_at": 1,
            "updated_at": 1700000000,
        }
        if prelabel_status is not None:
            item["prelabel_status"] = prelabel_status
        if prelabel_status == "Failed":
            item["prelabel_error"] = error or "model failure"
            item["autolabel_error"] = error or "model failure"
        self.stack.tables.labeling_tasks.put_item(Item=item)

    # ------------------------------------------------------------ invoke
    def rerun(self, job_id, user, body=None):
        """POST /labeling/{id}/rerun-prelabels through the real router;
        body None = bodyless request."""
        event = {
            "httpMethod": "POST",
            "resource": "/labeling/{id}/rerun-prelabels",
            "path": f"/labeling/{job_id}/rerun-prelabels",
            "pathParameters": {"id": job_id},
            "queryStringParameters": None,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "sub": user["user_id"],
                        "email": user["email"],
                        "cognito:username": user["username"],
                        "custom:role": user["role"],
                    }
                }
            },
        }
        response = self.dda.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    # ------------------------------------------------------------- store
    def get_job(self, job_id):
        return self.stack.tables.labeling_jobs.get_item(
            Key={"job_id": job_id}).get("Item")

    def tasks_sorted(self, job_id):
        items = self.stack.tables.labeling_tasks.query(
            KeyConditionExpression=Key("job_id").eq(job_id),
        ).get("Items", [])
        return sorted(items, key=lambda item: item["task_id"])

    def rerun_audit_events(self, job_id):
        response = self.stack.tables.audit_log.scan()
        return [item for item in response.get("Items", [])
                if item.get("action") == "prelabels_rerun"
                and item.get("resource_id") == job_id]


# -------------------------------------------------------------- generators

# Printable unicode without surrogates, with and without the ASCII
# period — the sibling property suites' alphabets.
_TEXT_ALPHABET = st.characters(min_codepoint=32, max_codepoint=0x2FFF,
                               blacklist_categories=("Cs",))
_CLEAN_ALPHABET = st.characters(min_codepoint=32, max_codepoint=0x2FFF,
                                blacklist_categories=("Cs",),
                                blacklist_characters=".")

# Period-free, strip-stable labels (seeded records hold pre-stripped
# names, as creation persists them).
_clean_labels = st.text(alphabet=_CLEAN_ALPHABET, min_size=1,
                        max_size=20).map(str.strip).filter(bool)

# Period-bearing labels: the incident-style versioned name, an inner
# period, a trailing period.
_period_labels = st.one_of(
    st.just("v1.2 defect"),
    st.builds(lambda left, right: f"{left}.{right}",
              _clean_labels, _clean_labels),
    st.builds(lambda core: f"{core}.", _clean_labels),
)

# Blank-after-trim values (dropped silently; the label-name fallback
# applies). "\u00a0" is unicode whitespace for str.strip().
_blank_values = st.sampled_from(("", " ", "   ", "\t", "\n \t ", "\u00a0"))

# Period-free surviving values, including whitespace-padded variants
# (survivors must persist character-for-character, padding included) and
# the punctuation the guardrail must not reject (comma, ?, !, ;, CJK 。).
_clean_value_core = st.one_of(
    st.sampled_from((CORRECTED_PROMPT,
                     "scratch, dent, or chip on the rim",
                     "is it damaged? yes! fine; ok",
                     "\u7f3a\u9677\u3002\u88c2\u7f1d")),
    st.text(alphabet=_CLEAN_ALPHABET, min_size=1,
            max_size=40).filter(lambda t: t.strip()),
)
_clean_values = st.one_of(
    _clean_value_core,
    st.builds(lambda core: f" {core} ", _clean_value_core),
)

# Period-bearing surviving values: the incident's instruction style, the
# trailing dot (stricter than the worker, by design), all-dots, a
# version string, arbitrary inner periods.
_period_values = st.one_of(
    st.sampled_from((INCIDENT_PROMPT, "scratch.", "...", "v1.2")),
    st.builds(lambda left, right: f"{left}.{right}",
              _clean_value_core, _clean_value_core),
)

# Raw length > 256 (creation's PROMPT_OVERRIDE_MAX_LENGTH restated),
# over the full alphabet so over-length wins over the period check.
_overlong_values = st.text(alphabet=_TEXT_ALPHABET, min_size=257,
                           max_size=270)

# JSON-expressible non-string values.
_nonstring_values = st.one_of(
    st.integers(min_value=-999, max_value=999),
    st.booleans(),
    st.none(),
    st.lists(st.integers(min_value=0, max_value=3), max_size=2),
)

_MODELS = ("grounded-sam", "sam", "bedrock:anthropic.claude-3-haiku",
           "llm:us.amazon.nova-pro-v1:0")
_OTHER_MODELS = ("sam", "bedrock:anthropic.claude-3-haiku",
                 "llm:us.amazon.nova-pro-v1:0")
_EXTRA_TASK_STATUSES = st.lists(
    st.sampled_from(("Available", "Pending", "none")), max_size=3)


@st.composite
def _eligibility_cases(draw):
    """One Property 4 job-record state. The 'eligible' kind keeps the
    acceptance branch meaningfully weighted (a free draw is eligible only
    ~5% of the time); 'free' spans the whole factor product. Labels stay
    period-free and no overrides are persisted, so the Req 5.6 persisted-
    guardrail gate (Property 5 territory) never fires here."""
    kind = draw(st.sampled_from(("free", "free", "eligible")))
    if kind == "eligible":
        exists, backend, status = True, "DDA", "InProgress"
        enabled = draw(st.booleans())
        skip_verification = draw(st.booleans()) if enabled else True
        review_finalized = draw(st.sampled_from(("absent", False)))
        failed_count = draw(st.integers(min_value=1, max_value=5))
    else:
        exists = draw(st.sampled_from((True, True, True, True, False)))
        backend = draw(st.sampled_from(("DDA", "GroundTruth")))
        status = draw(st.sampled_from(
            ("InProgress", "Stopped", "Completed", "Failed")))
        enabled = draw(st.booleans())
        skip_verification = draw(st.booleans())
        review_finalized = draw(st.sampled_from(("absent", False, True)))
        failed_count = draw(st.one_of(st.sampled_from((0, 1)),
                                      st.integers(min_value=2, max_value=5)))
    return SimpleNamespace(
        exists=exists, backend=backend, status=status, enabled=enabled,
        skip_verification=skip_verification,
        review_finalized=review_finalized,
        model=draw(st.sampled_from(_MODELS)),
        labels=draw(st.lists(_clean_labels, min_size=1, max_size=3,
                             unique=True)),
        failed_count=failed_count,
        extra_statuses=tuple(draw(_EXTRA_TASK_STATUSES)),
    )


@st.composite
def _grounded_sam_override_cases(draw):
    """One Property 5 grounded-sam scenario over a Retry_Eligible job:
    labels with and without periods, persisted override states (absent /
    clean / period-bearing / blank), and a body that is absent or a map
    mixing valid, blank, period-bearing, over-length, non-string, and
    unknown-key entries (submitted None = bodyless request)."""
    labels = draw(st.lists(
        st.one_of(_clean_labels, _clean_labels, _period_labels),
        min_size=1, max_size=4, unique=True))

    persisted = None
    if draw(st.booleans()):
        persisted = {}
        for label in labels:
            state = draw(st.sampled_from(
                ("absent", "clean", "period", "blank")))
            if state == "clean":
                persisted[label] = draw(_clean_values)
            elif state == "period":
                persisted[label] = draw(_period_values)
            elif state == "blank":
                persisted[label] = draw(_blank_values)
        if not persisted:
            persisted = None  # creation never persists an empty map

    submitted = None
    if draw(st.booleans()):
        submitted = {}
        for label in labels:
            state = draw(st.sampled_from(
                ("absent", "valid", "valid", "blank", "period",
                 "overlong", "nonstring")))
            if state == "valid":
                submitted[label] = draw(_clean_values)
            elif state == "blank":
                submitted[label] = draw(_blank_values)
            elif state == "period":
                submitted[label] = draw(_period_values)
            elif state == "overlong":
                submitted[label] = draw(_overlong_values)
            elif state == "nonstring":
                submitted[label] = draw(_nonstring_values)
        for key in draw(st.lists(_clean_labels, max_size=2, unique=True)):
            if key in labels or key in submitted:
                continue
            submitted[key] = draw(st.one_of(
                _clean_values, _period_values, _overlong_values,
                _nonstring_values))

    return SimpleNamespace(
        model="grounded-sam", labels=labels, persisted=persisted,
        submitted=submitted,
        failed_count=draw(st.integers(min_value=1, max_value=3)),
        extra_statuses=tuple(draw(st.lists(
            st.sampled_from(("Available", "Pending", "none")),
            max_size=2))),
    )


@st.composite
def _other_family_override_cases(draw):
    """One Property 5 non-grounded-sam scenario: a Retry_Eligible sam /
    bedrock: / llm: job whose Retry_Request carries a `prompt_overrides`
    body (any content, even empty) — always rejected (Req 5.7)."""
    labels = draw(st.lists(_clean_labels, min_size=1, max_size=3,
                           unique=True))
    submitted = {}
    for label in labels:
        if draw(st.booleans()):
            submitted[label] = draw(st.one_of(_clean_values,
                                              _period_values))
    return SimpleNamespace(
        model=draw(st.sampled_from(_OTHER_MODELS)), labels=labels,
        persisted=None, submitted=submitted,
        failed_count=draw(st.integers(min_value=1, max_value=3)),
        extra_statuses=(),
    )


_override_cases = st.one_of(_grounded_sam_override_cases(),
                            _grounded_sam_override_cases(),
                            _other_family_override_cases())


# ----------------------------------------------------------------- oracles

def _expected_rejection(case):
    """(status, error fragment) for the FIRST unmet eligibility gate in
    the design's documented order, or None when the job is a
    Retry_Eligible_Job (the requirements-glossary predicate restated)."""
    if not case.exists:
        return 404, "not found"
    if case.backend != "DDA":
        return 400, "DDA labeling jobs"
    if case.status != "InProgress":
        return 400, "InProgress"
    if not (case.enabled or case.skip_verification):
        return 400, "auto-labeling enabled"
    if case.review_finalized is True:
        return 400, "finalized"
    if case.failed_count < 1:
        return 400, "no failed pre-label tasks"
    return None


def _surviving_overrides(submitted, labels):
    """Creation's override rules restated: survivors are exactly the
    submitted entries whose key belongs to the Label_Set, whose value is
    a string of raw length at most 256, and which are non-blank after
    trimming — values kept character-for-character."""
    return {key: value for key, value in submitted.items()
            if key in labels and isinstance(value, str)
            and len(value) <= 256 and value.strip()}


def _expected_offenses(case):
    """The Property 5 oracle: [(label, kind)] the rejection must
    enumerate, [] meaning the request is accepted. Kinds: unknown_key /
    non_string / over_length (creation's per-entry rules, first offense
    per entry), then the guardrail over the resulting Effective_Prompts
    (submitted survivors when a body was sent, else the persisted map,
    else the label-name fallback) — override_period / label_period."""
    if case.model != "grounded-sam":
        return [(None, "not_grounded_sam")]
    expected = []
    if case.submitted is not None:
        for key, value in case.submitted.items():
            if key not in case.labels:
                expected.append((key, "unknown_key"))
            elif not isinstance(value, str):
                expected.append((key, "non_string"))
            elif len(value) > 256:
                expected.append((key, "over_length"))
        effective = _surviving_overrides(case.submitted, case.labels)
    else:
        effective = case.persisted or {}
    for label in case.labels:
        value = effective.get(label)
        if isinstance(value, str) and value.strip():
            if "." in value:
                expected.append((label, "override_period"))
        elif "." in label:
            expected.append((label, "label_period"))
    return expected


def _classify_error(error):
    """(label, kind) for one validation_errors entry, keyed on the
    creation-identical wording (Req 5.5). 'has no text prompt' must be
    checked before 'contains a period': the label-source message
    contains both fragments."""
    message = error.get("message", "")
    if "apply only to grounded-sam" in message:
        return (None, "not_grounded_sam")
    if "is not a label" in message:
        return (error.get("label"), "unknown_key")
    if "must be text" in message:
        return (error.get("label"), "non_string")
    if "must be at most" in message:
        return (error.get("label"), "over_length")
    if "has no text prompt" in message:
        return (error.get("label"), "label_period")
    if "contains a period" in message:
        return (error.get("label"), "override_period")
    return (error.get("label"), f"unrecognized: {message}")


def _persistence_normalized(job):
    """The job record with the two attributes an accepted override
    submission may legitimately write (auto_label.prompt_overrides,
    updated_at) masked out — everything else must be untouched."""
    item = deepcopy(job)
    item.pop("updated_at", None)
    auto_label = dict(item.get("auto_label") or {})
    auto_label.pop("prompt_overrides", None)
    item["auto_label"] = auto_label
    return item


# =========================================================================== #
# Property 4: Retry is accepted iff the job is Retry_Eligible, and
# rejection mutates nothing (route arm)
# =========================================================================== #

class TestProperty4RetryAcceptedIffRetryEligible:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_eligibility_cases())
    @example(case=SimpleNamespace(          # fully eligible, several failed
        exists=True, backend="DDA", status="InProgress", enabled=True,
        skip_verification=False, review_finalized="absent",
        model="grounded-sam", labels=["scratch"], failed_count=3,
        extra_statuses=("Available", "Pending")))
    @example(case=SimpleNamespace(          # eligible skip-verification job
        exists=True, backend="DDA", status="InProgress", enabled=False,
        skip_verification=True, review_finalized=False,
        model="bedrock:anthropic.claude-3-haiku", labels=["normal"],
        failed_count=1, extra_statuses=()))
    @example(case=SimpleNamespace(          # Ground Truth backend
        exists=True, backend="GroundTruth", status="InProgress",
        enabled=True, skip_verification=False, review_finalized="absent",
        model="sam", labels=["scratch"], failed_count=2,
        extra_statuses=()))
    @example(case=SimpleNamespace(          # stopped job
        exists=True, backend="DDA", status="Stopped", enabled=True,
        skip_verification=False, review_finalized="absent", model="sam",
        labels=["scratch"], failed_count=2, extra_statuses=("none",)))
    @example(case=SimpleNamespace(          # auto-labeling off
        exists=True, backend="DDA", status="InProgress", enabled=False,
        skip_verification=False, review_finalized="absent", model="sam",
        labels=["scratch"], failed_count=2, extra_statuses=()))
    @example(case=SimpleNamespace(          # review finalized
        exists=True, backend="DDA", status="InProgress", enabled=False,
        skip_verification=True, review_finalized=True,
        model="bedrock:anthropic.claude-3-haiku", labels=["normal"],
        failed_count=2, extra_statuses=()))
    @example(case=SimpleNamespace(          # zero failed tasks
        exists=True, backend="DDA", status="InProgress", enabled=True,
        skip_verification=False, review_finalized="absent",
        model="llm:us.amazon.nova-pro-v1:0", labels=["scratch"],
        failed_count=0, extra_statuses=("Available",)))
    @example(case=SimpleNamespace(          # missing job -> 404
        exists=False, backend="DDA", status="InProgress", enabled=True,
        skip_verification=False, review_finalized="absent", model="sam",
        labels=["scratch"], failed_count=1, extra_statuses=()))
    def test_property_retry_accepted_iff_retry_eligible(
            self, aws_stack, dda, case):
        """Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
        Property 4: Retry is accepted iff the job is Retry_Eligible, and
        rejection mutates nothing — *For any* job record state (backend,
        status, `review_finalized`, `auto_label.enabled`,
        `skip_verification`, Failed-task count) and an authorized caller
        on the route, the retry SHALL proceed exactly when the job is a
        Retry_Eligible_Job; every rejection SHALL name the unmet
        condition (404 for a missing job) and SHALL leave the job record,
        every task item, the fake Lambda invocations, and the audit
        trail's non-denial entries unchanged.

        **Validates: Requirements 5.4, 6.8**
        """
        env = RetryRouteEnv(aws_stack, dda)
        if case.exists:
            auto_label = {"enabled": case.enabled}
            if case.enabled or case.skip_verification:
                auto_label["model"] = case.model
            job_id = env.put_job(
                backend=case.backend, status=case.status,
                auto_label=auto_label,
                skip_verification=case.skip_verification,
                review_finalized=case.review_finalized,
                label_set=case.labels)
            env.put_tasks(job_id, case.failed_count, case.extra_statuses)
        else:
            job_id = f"labeling-{uuid.uuid4().hex[:10]}"
        # Skip-verification jobs are driven by an admin so the Req 5.3
        # role gate (an example-tested branch) never masks eligibility.
        caller = (env.admin if case.exists and case.skip_verification
                  else env.manager)

        job_before = env.get_job(job_id)
        tasks_before = env.tasks_sorted(job_id)
        invocations_before = len(dda.lambda_client.invocations)

        status, body = env.rerun(job_id, user=caller)

        rejection = _expected_rejection(case)
        if rejection is None:
            # ---- accepted exactly when Retry_Eligible ----------------
            assert status == 202, (
                f"Retry_Eligible job rejected ({status}): {body!r}")
            assert body["job_id"] == job_id
            assert body["retried_count"] == case.failed_count
            new_invocations = dda.lambda_client.invocations[
                invocations_before:]
            assert len(new_invocations) == 1, (
                f"expected exactly one worker invocation: "
                f"{new_invocations!r}")
            assert (new_invocations[0]["FunctionName"]
                    == WORKER_FUNCTION_NAME)
            assert new_invocations[0]["InvocationType"] == "Event"
            assert json.loads(new_invocations[0]["Payload"]) == {
                "action": "retry_prelabels", "job_id": job_id}
            # A bodyless acceptance triggers the worker and mutates
            # nothing itself (the reset belongs to the Retry_Action).
            assert env.get_job(job_id) == job_before
            assert env.tasks_sorted(job_id) == tasks_before
        else:
            # ---- rejected naming the unmet condition -----------------
            expected_status, fragment = rejection
            assert status == expected_status, (
                f"expected {expected_status} for "
                f"{case!r}, got {status}: {body!r}")
            assert fragment in body["error"], (
                f"rejection does not name the unmet condition "
                f"({fragment!r}): {body!r}")
            # ---- and mutates nothing ---------------------------------
            assert env.get_job(job_id) == job_before, (
                "rejection changed the job record")
            assert env.tasks_sorted(job_id) == tasks_before, (
                "rejection changed a task item")
            assert (len(dda.lambda_client.invocations)
                    == invocations_before), (
                "rejection invoked the labeling worker")
            assert env.rerun_audit_events(job_id) == [], (
                "rejection wrote a prelabels_rerun audit event")


# =========================================================================== #
# Property 5: Retry override validation equals creation's rules and
# persists before triggering; rejection leaves record, tasks, and queue
# untouched
# =========================================================================== #

class TestProperty5OverrideValidationEqualsCreation:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_override_cases)
    @example(case=SimpleNamespace(          # the incident: bodyless retry
        model="grounded-sam", labels=["cookie_gap"],  # of the persisted
        persisted={"cookie_gap": INCIDENT_PROMPT},    # broken prompt
        submitted=None, failed_count=2, extra_statuses=()))
    @example(case=SimpleNamespace(          # the incident recovery:
        model="grounded-sam", labels=["cookie_gap"],  # corrected noun
        persisted={"cookie_gap": INCIDENT_PROMPT},    # phrase accepted
        submitted={"cookie_gap": CORRECTED_PROMPT},
        failed_count=2, extra_statuses=()))
    @example(case=SimpleNamespace(          # empty map clears overrides
        model="grounded-sam", labels=["scratch"],     # (key REMOVEd)
        persisted={"scratch": "long thin mark"}, submitted={},
        failed_count=1, extra_statuses=("Available",)))
    @example(case=SimpleNamespace(          # period label rescued by a
        model="grounded-sam", labels=["v1.2 defect"],  # clean override
        persisted=None, submitted={"v1.2 defect": "vee defect"},
        failed_count=1, extra_statuses=()))
    @example(case=SimpleNamespace(          # period label + blank value:
        model="grounded-sam", labels=["v1.2 defect"],  # fallback offends
        persisted=None, submitted={"v1.2 defect": "   "},
        failed_count=1, extra_statuses=()))
    @example(case=SimpleNamespace(          # every offense kind at once
        model="grounded-sam", labels=["rim", "v1.2 defect"],
        persisted={"rim": "clean value"},
        submitted={"rim": "scratch.", "v1.2 defect": 7,
                   "ghost": "x", "long": "y" * 257},
        failed_count=1, extra_statuses=()))
    @example(case=SimpleNamespace(          # persisted overrides judged
        model="grounded-sam", labels=["rim", "gap"],  # on bodyless retry
        persisted={"rim": "scratch, dent; fine", "gap": "a. b"},
        submitted=None, failed_count=1, extra_statuses=()))
    @example(case=SimpleNamespace(          # llm job with an overrides
        model="llm:us.amazon.nova-pro-v1:0",          # body -> 400
        labels=["scratch"], persisted=None,
        submitted={"scratch": "clean value"}, failed_count=1,
        extra_statuses=()))
    def test_property_override_validation_equals_creation_and_persists_before_trigger(
            self, aws_stack, dda, case):
        """Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
        Property 5: Retry override validation equals creation's rules and
        persists before triggering; rejection leaves record, tasks, and
        queue untouched — *For any* grounded-sam Retry_Request body
        (`prompt_overrides` absent, or maps mixing valid, blank,
        period-bearing, over-length, unknown-key, and non-string values)
        and *any* persisted override state (including period-bearing
        persisted overrides and period-bearing labels), the request SHALL
        be accepted exactly when creation's override rules and the
        guardrail hold over the resulting Effective_Prompts (submitted
        survivors, else persisted survivors, else label names);
        acceptance SHALL persist the surviving submitted overrides
        character-for-character (key removed when none survives) before
        the worker invocation; rejection SHALL enumerate the offenses and
        change nothing; and *for any* non-grounded-sam job a body
        carrying `prompt_overrides` SHALL be rejected.

        **Validates: Requirements 5.5, 5.6, 5.7, 5.8**
        """
        env = RetryRouteEnv(aws_stack, dda)
        auto_label = {"enabled": True, "model": case.model}
        if case.persisted is not None:
            auto_label["prompt_overrides"] = dict(case.persisted)
        job_id = env.put_job(auto_label=auto_label, label_set=case.labels)
        env.put_tasks(job_id, case.failed_count, case.extra_statuses)

        job_before = env.get_job(job_id)
        tasks_before = env.tasks_sorted(job_id)
        invocations_before = len(dda.lambda_client.invocations)

        body_payload = (None if case.submitted is None
                        else {"prompt_overrides": case.submitted})
        status, body = env.rerun(job_id, user=env.manager,
                                 body=body_payload)

        expected = _expected_offenses(case)
        if not expected:
            # ---- accepted exactly when creation's rules + guardrail
            # hold over the resulting Effective_Prompts ----------------
            assert status == 202, (
                f"valid retry rejected (labels={case.labels!r}, "
                f"submitted={case.submitted!r}, "
                f"persisted={case.persisted!r}): {body!r}")
            assert body["job_id"] == job_id
            assert body["retried_count"] == case.failed_count
            new_invocations = dda.lambda_client.invocations[
                invocations_before:]
            assert len(new_invocations) == 1
            assert json.loads(new_invocations[0]["Payload"]) == {
                "action": "retry_prelabels", "job_id": job_id}

            job_after = env.get_job(job_id)
            invoke_snapshot = dda.lambda_client.job_snapshots[
                invocations_before]
            if case.submitted is not None:
                # Surviving submitted overrides persisted character-for-
                # character (key REMOVEd when none survives), already in
                # place at invoke time (persist-before-trigger, Req 5.5).
                survivors = _surviving_overrides(case.submitted,
                                                 case.labels)
                for record in (invoke_snapshot, job_after):
                    stored = record["auto_label"]
                    if survivors:
                        assert stored.get("prompt_overrides") == survivors, (
                            f"persisted overrides drifted from the "
                            f"submitted survivors: "
                            f"{stored.get('prompt_overrides')!r} != "
                            f"{survivors!r}")
                    else:
                        assert "prompt_overrides" not in stored, (
                            f"empty survivor set must REMOVE the key: "
                            f"{stored!r}")
                # Nothing else on the record changed.
                assert (_persistence_normalized(job_after)
                        == _persistence_normalized(job_before)), (
                    "an accepted override submission changed more than "
                    "auto_label.prompt_overrides/updated_at")
            else:
                # Bodyless acceptance: the record is byte-identical, at
                # invoke time and after (Req 5.6 passed on persisted
                # prompts; nothing persisted anew).
                assert invoke_snapshot == job_before
                assert job_after == job_before
        else:
            # ---- rejected enumerating the offenses per label ---------
            assert status == 400, (
                f"invalid retry accepted (labels={case.labels!r}, "
                f"submitted={case.submitted!r}, "
                f"persisted={case.persisted!r}): {status} {body!r}")
            errors = body["validation_errors"]
            actual = [_classify_error(error) for error in errors]
            assert (sorted(actual, key=repr)
                    == sorted(expected, key=repr)), (
                f"offense enumeration drifted: expected {expected!r}, "
                f"got {actual!r} from {errors!r}")
            for error in errors:
                label, kind = _classify_error(error)
                if kind == "not_grounded_sam":
                    assert error["parameter"] == "prompt_overrides"
                else:
                    # Creation-identical wording names each offending
                    # label (Req 5.5).
                    assert error["parameter"] == "auto_label"
                    assert f"'{label}'" in error["message"], (
                        f"error does not name the label {label!r}: "
                        f"{error['message']!r}")
            # ---- and changes nothing (Req 5.8) -----------------------
            assert env.get_job(job_id) == job_before, (
                "rejection changed the job record")
            assert env.tasks_sorted(job_id) == tasks_before, (
                "rejection changed a task item")
            assert (len(dda.lambda_client.invocations)
                    == invocations_before), (
                "rejection invoked the labeling worker")
            assert env.rerun_audit_events(job_id) == [], (
                "rejection wrote a prelabels_rerun audit event")
