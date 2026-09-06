"""
Prompt_Guardrail job-creation property test.

Spec: grounded-sam-prompt-guardrails-and-prelabel-retry, task 1.2.

One property over the real `create_dda_job` in dda_labeling.py, driven
against the moto-backed stack (the test_dda_labeling_create_job.py
scaffolding: real shared_utils / rbac path, moto DynamoDB + S3, fake
Cognito and Lambda clients), 100 Hypothesis examples:

**Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
Property 2: Creation accepts iff the guardrail holds, enumerating
offenders, records pre-feature-identical on acceptance** — *For any*
labeling job submission (family drawn from grounded-sam/sam/bedrock:/
llm:; labels and override values with and without periods), creation
SHALL be rejected exactly when the family is `grounded-sam` and some
Effective_Prompt contains a period — the rejection enumerating one
validation error per offending label naming it and its source,
persisting nothing — and SHALL otherwise accept with a job record equal
to the pre-feature creation rules' record for the same submission.
**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 8.2, 8.5**

Oracles
-------
Restated in this file, never imported from the code under test:

- The guardrail oracle is the design's Data Models oracle verbatim:
  for labels ``L`` and override map ``O``, survivors are exactly the
  entries non-blank after ``str.strip()``; ``effective(l)`` is the
  survivor when one exists, else the label name itself; the submission
  is invalid iff the family is ``grounded-sam`` AND some label's
  effective prompt contains the ASCII period ``'.'``. Each offender is
  reported with its source — a period-bearing surviving override
  ("contains a period", never "has no text prompt") vs a period-bearing
  label name left as the fallback prompt ("has no text prompt").
- The acceptance record oracle restates the pre-feature creation shape
  (the test_property_grounded_sam_job_creation.py record-shape style):
  the fixed dda-data-labeling top-level key set plus ``team_id``, and
  the per-family ``auto_label`` document — ``{enabled, model}`` for
  ``sam``/``bedrock:``, ``{enabled, model, detection_prompt}`` for
  ``llm:``, and for ``grounded-sam`` a ``prompt_overrides`` key present
  only when at least one submitted override survives trimming, equal to
  the survivors character-for-character. Non-grounded-sam records never
  carry ``prompt_overrides`` anywhere, even when the submission plants
  one (Req 8.2).

Generator domains: label sets and override values are drawn with and
without periods — the scenario kinds force period-bearing overrides,
period-bearing labels with blank/absent overrides, and fully clean
submissions, so both acceptance branches carry meaningful weight. The
clean space includes commas, question marks, exclamation points,
semicolons, and the CJK full stop ``。`` — none of which is in the
worker's separator set, so none may be rejected (Req 2.4, 2.6).

Harness reuse (Hypothesis cannot consume function-scoped fixtures): the
module-scoped `dda` fixture follows
test_property_grounded_sam_job_creation.py; per-example environments
are built inside the test body from `CreateJobEnv`.
"""
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from test_dda_labeling_create_job import (
    DATASET_BUCKET,
    POOL_ID,
    REGION,
    CreateJobEnv,
    FakeCognitoClient,
    FakeLambdaClient,
)

GEOMETRY_MODALITIES = ("Segmentation", "ObjectDetection")

# Restated pre-feature shapes (dda-data-labeling Req 4.11/11.3/12.8 as
# pinned by test_dda_labeling_create_job.TestSuccessfulCreation) —
# deliberately not imported from dda_labeling so the oracle cannot
# drift with the code under test. Ordinary (non-skip-verification)
# jobs add team_id.
PRE_FEATURE_RECORD_KEYS = frozenset({
    "job_id", "usecase_id", "job_name", "labeling_backend", "status",
    "task_type", "label_set", "dataset_prefix", "dataset_bucket",
    "image_count", "skipped_object_count", "instructions", "example_images",
    "auto_label", "skip_verification", "submitted_count", "blocked",
    "created_at", "updated_at", "created_by",
})
CLASSIFICATION_LABELS = ["normal", "anomaly"]


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock, with
    fake Cognito and Lambda clients, plus the dataset bucket — the
    test_dda_labeling_create_job convention."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling

    fake_cognito = FakeCognitoClient()
    dda_labeling.cognito_client = fake_cognito
    dda_labeling.USER_POOL_ID = POOL_ID

    fake_lambda = FakeLambdaClient()
    dda_labeling.lambda_client = fake_lambda

    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=DATASET_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass

    return SimpleNamespace(module=dda_labeling, cognito=fake_cognito,
                           lambda_client=fake_lambda)


# -------------------------------------------------------------- generators

# Printable unicode without surrogates — the sibling property suites'
# alphabet (periods included: legal in detection prompts and in
# non-grounded-sam values).
_TEXT_ALPHABET = st.characters(min_codepoint=32, max_codepoint=0x2FFF,
                               blacklist_categories=("Cs",))

# The same alphabet with the ASCII period excluded — the post-guardrail
# clean space for grounded-sam labels and override values. Only U+002E
# breaks caption alignment; every other punctuation mark stays legal.
_CLEAN_ALPHABET = st.characters(min_codepoint=32, max_codepoint=0x2FFF,
                                blacklist_categories=("Cs",),
                                blacklist_characters=".")

# Period-free labels: 1-24 characters, non-empty and stable under strip
# (the backend persists stripped names; pre-stripped labels keep the
# submitted set equal to the persisted set the override keys and the
# guardrail's fallback are judged against).
_clean_labels = st.text(alphabet=_CLEAN_ALPHABET, min_size=1,
                        max_size=24).map(str.strip).filter(bool)

# Period-bearing labels (<= 49 characters, still strip-stable): the
# incident-style versioned name, an inner period, a trailing period.
_period_labels = st.one_of(
    st.just("v1.2 defect"),
    st.builds(lambda left, right: f"{left}.{right}",
              _clean_labels, _clean_labels),
    st.builds(lambda core: f"{core}.", _clean_labels),
)

_label_pool = st.one_of(_clean_labels, _period_labels)

# Blank-after-trim override values (dropped silently, so the label-name
# fallback applies). "\u00a0" (NBSP) is unicode whitespace.
_blank_values = st.sampled_from(["", " ", "   ", "\t", "\n \t ", "\u00a0"])

# Clean (period-free, surviving) override values. Commas, question
# marks, exclamation points, semicolons, and the CJK full stop tokenize
# as ordinary tokens — they extend a caption span rather than splitting
# it, so they belong to the accepted space (Req 2.4).
_clean_values = st.one_of(
    st.sampled_from([
        "gap between broken cookie pieces",
        "scratch, dent, or chip on the rim",
        "is there a defect?",
        "watch out! sharp edge",
        "first phrase; second phrase",
        "\u7f3a\u9677\u3002\u88c2\u7f1d",  # CJK full stop 。 is not '.'
    ]),
    st.text(alphabet=_CLEAN_ALPHABET, min_size=1,
            max_size=40).filter(lambda t: t.strip()),
)

# Period-bearing (surviving — any value containing '.' is non-blank
# after trim) override values: the incident's instruction style, the
# trailing dot the worker would tolerate but the guardrail still
# rejects, all-dots, a version string, and arbitrary inner periods.
_period_values = st.one_of(
    st.sampled_from([
        "draw and fill in the gaps in the image. If there is a large "
        "crack, fill it in",
        "scratch.",
        "...",
        "v1.2",
    ]),
    st.builds(lambda left, right: f"{left}.{right}",
              _clean_values, _clean_values),
)

# Per-label override entry states.
_ANY_STATE = st.sampled_from(("absent", "blank", "clean", "period"))
_CLEAN_STATES = st.sampled_from(("absent", "blank", "clean"))
_FALLBACK_STATES = st.sampled_from(("absent", "blank"))


@st.composite
def _grounded_sam_cases(draw):
    """One grounded-sam submission, valid on every pre-existing rule
    (in-Label_Set string keys, raw length far below 256), drawn from a
    scenario kind that keeps every guardrail branch meaningfully
    weighted: fully clean, a forced period-bearing surviving override,
    a forced period-bearing label with a blank/absent override, or a
    free mix the oracle adjudicates."""
    modality = draw(st.sampled_from(GEOMETRY_MODALITIES))
    kind = draw(st.sampled_from(
        ("clean", "clean", "override_offender", "label_offender", "mixed")))

    if kind == "clean":
        labels = draw(st.lists(_clean_labels, min_size=1, max_size=4,
                               unique=True))
        states = {label: draw(_CLEAN_STATES) for label in labels}
    elif kind == "override_offender":
        labels = draw(st.lists(_label_pool, min_size=1, max_size=4,
                               unique=True))
        forced = draw(st.sampled_from(labels))
        states = {label: ("period" if label == forced
                          else draw(_ANY_STATE)) for label in labels}
    elif kind == "label_offender":
        clean_part = draw(st.lists(_clean_labels, min_size=0, max_size=3,
                                   unique=True))
        offender = draw(_period_labels)  # contains '.', never collides
        position = draw(st.integers(min_value=0,
                                    max_value=len(clean_part)))
        labels = clean_part[:position] + [offender] + clean_part[position:]
        states = {label: (draw(_FALLBACK_STATES) if label == offender
                          else draw(_ANY_STATE)) for label in labels}
    else:  # mixed — anything; the oracle decides
        labels = draw(st.lists(_label_pool, min_size=1, max_size=4,
                               unique=True))
        states = {label: draw(_ANY_STATE) for label in labels}

    values = {}
    for label, state in states.items():
        if state == "blank":
            values[label] = draw(_blank_values)
        elif state == "clean":
            values[label] = draw(_clean_values)
        elif state == "period":
            values[label] = draw(_period_values)
    if values:
        overrides = values
    else:  # nothing to carry: absent key or the present-but-empty map
        overrides = draw(st.sampled_from((None, {})))
        overrides = dict(overrides) if overrides is not None else None

    return SimpleNamespace(family="grounded-sam", model="grounded-sam",
                           modality=modality, labels=labels,
                           overrides=overrides, detection_prompt=None)


@st.composite
def _other_family_cases(draw):
    """One valid submission of another family (sam / bedrock: / llm:,
    each over its pre-feature modality matrix), adversarially loaded
    with periods everywhere the guardrail must ignore: period-bearing
    labels, a planted period-bearing prompt_overrides value, a
    period-bearing detection_prompt, and model identifiers that
    themselves contain periods (Req 2.6, 8.2)."""
    family = draw(st.sampled_from(("sam", "bedrock", "llm")))
    if family == "sam":
        model = "sam"
        modality = draw(st.sampled_from(GEOMETRY_MODALITIES))
    elif family == "bedrock":
        model = "bedrock:anthropic.claude-3-haiku"
        modality = draw(st.sampled_from(("Classification",
                                         "ObjectDetection")))
    else:
        model = "llm:us.amazon.nova-pro-v1:0"
        modality = draw(st.sampled_from(("Classification", "Segmentation",
                                         "ObjectDetection")))
    labels = (None if modality == "Classification"
              else draw(st.lists(_label_pool, min_size=1, max_size=4,
                                 unique=True)))

    # Planted stray prompt_overrides: ignored by the pre-feature rules,
    # so the guardrail must neither reject it nor let it reach the
    # record — even period-bearing (None = nothing planted).
    planted = None
    if draw(st.booleans()):
        pool = labels if labels else list(CLASSIFICATION_LABELS)
        keys = draw(st.lists(st.sampled_from(pool), unique=True,
                             max_size=len(pool)))
        planted = {key: draw(st.one_of(_period_values, _clean_values))
                   for key in keys}

    detection_prompt = None
    if family == "llm":
        # Periods are legal in a Detection_Prompt — the guardrail is
        # scoped to grounded-sam Effective_Prompts alone.
        detection_prompt = draw(st.one_of(
            st.just("Find every defect. Mark each one."),
            st.text(alphabet=_TEXT_ALPHABET, min_size=1,
                    max_size=80).filter(lambda t: t.strip()),
        ))

    return SimpleNamespace(family=family, model=model, modality=modality,
                           labels=labels, overrides=planted,
                           detection_prompt=detection_prompt)


_cases = st.one_of(_grounded_sam_cases(), _other_family_cases())


# ----------------------------------------------------------------- oracles

def _surviving_overrides(overrides):
    """The Data Models oracle's survivor set: exactly the submitted
    entries non-empty after trimming, values character-for-character."""
    return {key: value for key, value in (overrides or {}).items()
            if value.strip()}


def _expected_offenders(labels, overrides):
    """The Data Models guardrail oracle: effective(l) = surviving
    override else label name; a label offends iff '.' is in its
    effective prompt. Returns [(label, source)] in Label_Set order,
    source 'override' when a surviving override carries the period,
    'label' when the period-bearing label name is the fallback."""
    survivors = _surviving_overrides(overrides)
    offenders = []
    for label in labels:
        if label in survivors:
            if "." in survivors[label]:
                offenders.append((label, "override"))
        elif "." in label:
            offenders.append((label, "label"))
    return offenders


def _contains_key_anywhere(value, key):
    """True when `key` appears as a mapping key anywhere in the nested
    document."""
    if isinstance(value, dict):
        if key in value:
            return True
        return any(_contains_key_anywhere(item, key)
                   for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_key_anywhere(item, key) for item in value)
    return False


def _error_names_label(message, label):
    """The rejection must name the offending label (Req 2.1, 2.2)."""
    return f"'{label}'" in message


# =========================================================================== #
# Property 2: Creation accepts iff the guardrail holds, enumerating
# offenders, records pre-feature-identical on acceptance
# =========================================================================== #

class TestProperty2CreationAcceptsIffGuardrailHolds:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_cases)
    @example(case=SimpleNamespace(          # the incident: instruction-style
        family="grounded-sam", model="grounded-sam",  # override, inner '.'
        modality="Segmentation", labels=["cookie_gap"],
        overrides={"cookie_gap": "draw and fill in the gaps in the image. "
                                 "If there is a large crack, fill it in"},
        detection_prompt=None))
    @example(case=SimpleNamespace(          # period label, no override map
        family="grounded-sam", model="grounded-sam",
        modality="ObjectDetection", labels=["v1.2 defect"],
        overrides=None, detection_prompt=None))
    @example(case=SimpleNamespace(          # period label, blank override
        family="grounded-sam", model="grounded-sam",
        modality="Segmentation", labels=["v1.2 defect"],
        overrides={"v1.2 defect": "   "}, detection_prompt=None))
    @example(case=SimpleNamespace(          # trailing dot still rejected
        family="grounded-sam", model="grounded-sam",  # (stricter than the
        modality="Segmentation", labels=["rim"],      # worker, by design)
        overrides={"rim": "scratch."}, detection_prompt=None))
    @example(case=SimpleNamespace(          # both sources enumerated at once
        family="grounded-sam", model="grounded-sam",
        modality="ObjectDetection", labels=["v1.2 defect", "scratch"],
        overrides={"scratch": "a scratch. long and thin"},
        detection_prompt=None))
    @example(case=SimpleNamespace(          # fully clean: safe punctuation
        family="grounded-sam", model="grounded-sam",
        modality="Segmentation", labels=["cookie gap", "rim chip"],
        overrides={"cookie gap": "scratch, dent; is it wide? yes!",
                   "rim chip": "\u7f3a\u9677\u3002\u88c2\u7f1d"},
        detection_prompt=None))
    @example(case=SimpleNamespace(          # period label rescued by a
        family="grounded-sam", model="grounded-sam",  # clean override
        modality="Segmentation", labels=["v1.2 defect"],
        overrides={"v1.2 defect": "vee one two defect"},
        detection_prompt=None))
    @example(case=SimpleNamespace(          # llm: periods everywhere the
        family="llm", model="llm:us.amazon.nova-pro-v1:0",  # guardrail
        modality="Classification", labels=None,             # must ignore
        overrides={"normal": "looks fine. no defect."},
        detection_prompt="Find every defect. Mark each one."))
    @example(case=SimpleNamespace(          # sam: period-bearing label
        family="sam", model="sam", modality="Segmentation",
        labels=["v1.2 defect"], overrides=None, detection_prompt=None))
    def test_property_creation_accepts_iff_guardrail_holds(
            self, aws_stack, dda, case):
        """Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
        Property 2: Creation accepts iff the guardrail holds, enumerating
        offenders, records pre-feature-identical on acceptance — *For any*
        labeling job submission (family drawn from grounded-sam/sam/
        bedrock:/llm:; labels and override values with and without
        periods), creation SHALL be rejected exactly when the family is
        `grounded-sam` and some Effective_Prompt contains a period — the
        rejection enumerating one validation error per offending label
        naming it and its source, persisting nothing — and SHALL otherwise
        accept with a job record equal to the pre-feature creation rules'
        record for the same submission.

        **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 8.2, 8.5**
        """
        env = CreateJobEnv(aws_stack, dda)
        env.put_images(["a.jpg"])
        job_name = f"job-{uuid.uuid4().hex[:12]}"

        auto_label = {"enabled": True, "model": case.model}
        if case.detection_prompt is not None:
            auto_label["detection_prompt"] = case.detection_prompt
        if case.overrides is not None:
            auto_label["prompt_overrides"] = dict(case.overrides)

        status, body = env.create(
            job_name=job_name, task_type=case.modality,
            label_set=(list(case.labels) if case.labels else None),
            auto_label=auto_label)

        # The guardrail oracle: only the grounded-sam family can offend
        # (Req 2.6 — other families are never rejected by the guardrail,
        # whatever periods their labels or planted overrides carry).
        offenders = (_expected_offenders(case.labels, case.overrides)
                     if case.family == "grounded-sam" else [])

        if offenders:
            # ---- rejected exactly when the oracle says invalid --------
            assert status == 400, (
                f"guardrail-violating submission accepted "
                f"(labels={case.labels!r}, overrides={case.overrides!r})")
            errors = body["validation_errors"]
            guardrail_errors = [error for error in errors
                                if "contains a period"
                                in error.get("message", "")]
            # The submission is valid on every pre-existing rule, so the
            # rejection is exactly the guardrail enumeration: one error
            # per offending label, no others (Req 2.3).
            assert guardrail_errors == errors, (
                f"rejection carries non-guardrail errors: {errors!r}")
            assert (sorted(error.get("label")
                           for error in guardrail_errors)
                    == sorted(label for label, _ in offenders)), (
                f"offender enumeration drifted: expected "
                f"{sorted(label for label, _ in offenders)!r}, got "
                f"{guardrail_errors!r}")
            for label, source in offenders:
                matching = [error for error in guardrail_errors
                            if error.get("label") == label]
                assert len(matching) == 1, (
                    f"expected exactly one error for {label!r}: "
                    f"{matching!r}")
                message = matching[0]["message"]
                assert _error_names_label(message, label), (
                    f"error does not name the label {label!r}: "
                    f"{message!r}")
                assert matching[0]["parameter"] == "auto_label"
                if source == "label":
                    # Label-name fallback offense: directs to an override.
                    assert "has no text prompt" in message, (
                        f"label-source offense not distinguished for "
                        f"{label!r}: {message!r}")
                else:
                    # Override offense: never the label-source wording.
                    assert "has no text prompt" not in message, (
                        f"override-source offense mislabeled for "
                        f"{label!r}: {message!r}")
            # ---- nothing persisted (Req 2.1, 2.2) ---------------------
            env.assert_nothing_persisted()
        else:
            # ---- accepted otherwise ----------------------------------
            assert status == 201, (
                f"guardrail-clean {case.family} submission rejected "
                f"(labels={case.labels!r}, overrides={case.overrides!r}): "
                f"{body!r}")
            job = env.get_job(body["job_id"])

            # Pre-feature top-level key set, exactly (Req 8.2, 8.5).
            expected_keys = set(PRE_FEATURE_RECORD_KEYS) | {"team_id"}
            assert set(job.keys()) == expected_keys, (
                f"record keys drifted from the pre-feature shape: "
                f"{sorted(set(job.keys()) ^ expected_keys)!r}")

            # The per-family pre-feature auto_label document; for
            # grounded-sam the surviving overrides char-for-char, the
            # key absent when none survives (Req 2.4).
            expected_auto_label = {"enabled": True, "model": case.model}
            if case.family == "llm":
                expected_auto_label["detection_prompt"] = (
                    case.detection_prompt)
            if case.family == "grounded-sam":
                survivors = _surviving_overrides(case.overrides)
                if survivors:
                    expected_auto_label["prompt_overrides"] = survivors
            assert job["auto_label"] == expected_auto_label, (
                f"auto_label drifted from the pre-feature document for "
                f"overrides={case.overrides!r}: {job['auto_label']!r}")

            # Req 8.2: a planted prompt_overrides value never reaches a
            # non-grounded-sam record, anywhere.
            if case.family != "grounded-sam":
                assert not _contains_key_anywhere(
                    job, "prompt_overrides"), (
                    f"prompt_overrides leaked into a {case.family} "
                    f"record (planted={case.overrides!r}): {job!r}")

            # Every other submission-derived field carries the
            # pre-feature value.
            expected_label_set = (list(CLASSIFICATION_LABELS)
                                  if case.modality == "Classification"
                                  else list(case.labels))
            assert job["job_id"] == body["job_id"]
            assert job["usecase_id"] == env.usecase_id
            assert job["job_name"] == job_name
            assert job["labeling_backend"] == "DDA"
            assert job["status"] == "InProgress"
            assert job["task_type"] == case.modality
            assert job["label_set"] == expected_label_set
            assert job["dataset_prefix"] == env.prefix
            assert job["dataset_bucket"] == DATASET_BUCKET
            assert job["image_count"] == 1
            assert job["skipped_object_count"] == 0
            assert job["instructions"] == ""
            assert job["example_images"] == {"good": [], "bad": []}
            assert job["skip_verification"] is False
            assert job["submitted_count"] == 0
            assert job["blocked"] is False
            assert job["created_by"] == env.creator["user_id"]
            assert job["team_id"] == env.team_id
