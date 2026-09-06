"""
Grounded-SAM job-creation property tests.

Spec: grounded-sam-autolabel, task 2.2.

Three properties over the real `create_dda_job` in dda_labeling.py,
driven against the moto-backed stack (the test_dda_labeling_create_job.py
scaffolding: real shared_utils / rbac path, moto DynamoDB + S3, fake
Cognito and Lambda clients), 100 Hypothesis examples per property:

**Feature: grounded-sam-autolabel, Property 3: Job creation persists the
model and the normalized overrides** — *For any* valid `grounded-sam`
submission (Segmentation or ObjectDetection, any Label_Set, any override
map of in-Label_Set keys with string values <= 256 characters mixing blank
and non-blank), the created job record SHALL carry `auto_label.model ==
'grounded-sam'` and `auto_label.prompt_overrides` equal to exactly the
non-blank-after-trim entries character-for-character, with the key absent
when none survives. **Validates: Requirements 1.5, 2.4**

**Feature: grounded-sam-autolabel, Property 4: Malformed overrides are
rejected and nothing persists** — *For any* `grounded-sam` submission whose
`prompt_overrides` is not an object of string values, carries a key outside
the submitted Label_Set, or carries a value whose raw length exceeds 256
characters, the creation request SHALL be rejected with a validation error
identifying the offense, and no job record SHALL be persisted.
**Validates: Requirements 2.5, 2.6**

**Feature: grounded-sam-autolabel, Property 15: Other families' job records
are byte-identical to pre-feature records** — *For any* valid submission of
the `sam`, `bedrock:` or `llm:` family (with and without skip-verification),
the created job record SHALL contain no `prompt_overrides` key anywhere and
SHALL equal, key for key, the record the pre-feature creation rules produce
for the same submission. **Validates: Requirements 2.8, 7.1**

Oracles
-------
Every oracle is restated in this file, never imported from the code under
test:

- Property 3's normalization oracle is the requirement itself: survivors =
  exactly the submitted entries non-empty after `str.strip()`, values
  compared character-for-character, the `prompt_overrides` key absent when
  no entry survives. The 256-character boundary is pinned inside the
  property's space (an explicit `@example` carries a raw-length-256 value).
- Property 4 pins raw length 257 as the first rejected length (`@example`)
  and asserts the offense-identifying error content per malformation kind.
- Property 15's record-shape oracle restates the pre-feature contract: the
  fixed top-level key set of the dda-data-labeling creation rules (plus
  `team_id` for ordinary jobs, plus `bedrock_model_id`/`per_label_prompts`
  for skip-verification jobs) and the per-family `auto_label` document from
  the design's data model — `{enabled, model}` for `sam`/`bedrock:`,
  `{enabled, model, detection_prompt}` for `llm:` (no few-shot or sizing
  values are submitted here), and never a `prompt_overrides` key. To make
  the differential adversarial, submissions may also *plant* a stray
  `prompt_overrides` value on `auto_label` — ignored pre-feature, so it
  must never reach the record post-feature either.

Harness reuse (Hypothesis cannot consume function-scoped fixtures): the
module-scoped `dda` fixture follows
test_property_unconfigured_sizing_preservation.py; per-example environments
are built inside the test bodies from `CreateJobEnv`.
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

# Restated bounds and shapes (grounded-sam-autolabel Req 2.6 and the
# dda-data-labeling creation contract) — deliberately not imported from
# dda_labeling so the oracle cannot drift with the code under test.
OVERRIDE_LENGTH_LIMIT = 256
CLASSIFICATION_LABELS = ["normal", "anomaly"]
BEDROCK_SKIP_MODEL = "anthropic.claude-3-haiku"

# The pre-feature top-level job-record key set (dda-data-labeling
# Req 4.11/11.3/12.8, as pinned by
# test_dda_labeling_create_job.TestSuccessfulCreation). Ordinary jobs add
# team_id; skip-verification jobs add bedrock_model_id/per_label_prompts.
PRE_FEATURE_RECORD_KEYS = frozenset({
    "job_id", "usecase_id", "job_name", "labeling_backend", "status",
    "task_type", "label_set", "dataset_prefix", "dataset_bucket",
    "image_count", "skipped_object_count", "instructions", "example_images",
    "auto_label", "skip_verification", "submitted_count", "blocked",
    "created_at", "updated_at", "created_by",
})


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

# Printable unicode without surrogates: Latin, Greek, Cyrillic, CJK
# punctuation, symbols — the alphabet the sibling property suites use.
_TEXT_ALPHABET = st.characters(min_codepoint=32, max_codepoint=0x2FFF,
                               blacklist_categories=("Cs",))

# Valid Label_Set names: 1-64 characters, non-empty and stable under
# strip (the backend persists stripped names; pre-stripped labels keep the
# submitted set equal to the persisted set the override keys are checked
# against).
_label_names = st.text(alphabet=_TEXT_ALPHABET, min_size=1,
                       max_size=64).map(str.strip).filter(bool)

_label_sets = st.lists(_label_names, min_size=1, max_size=5, unique=True)

# Blank-after-trim override values (dropped silently per Req 2.4).
# "\u00a0" (NBSP) is unicode whitespace, so it is blank under str.strip.
_blank_values = st.sampled_from(["", " ", "   ", "\t", "\n \t ", "\u00a0"])

# Non-blank values of raw length <= 256, mixing unicode, inner/outer
# whitespace (which must survive character-for-character) and the
# 256-character boundary itself.
_nonblank_values = st.one_of(
    st.text(alphabet=_TEXT_ALPHABET, min_size=1,
            max_size=OVERRIDE_LENGTH_LIMIT).filter(lambda t: t.strip()),
    st.just("x" * OVERRIDE_LENGTH_LIMIT),
    st.just("\u00fc" * OVERRIDE_LENGTH_LIMIT),
    st.builds(lambda core: f"  {core}  ",
              st.text(alphabet=_TEXT_ALPHABET, min_size=1,
                      max_size=OVERRIDE_LENGTH_LIMIT - 4).filter(
                          lambda t: t.strip())),
)

_override_values = st.one_of(_blank_values, _nonblank_values)

# Non-objects for the prompt_overrides value (None means "absent", so it
# is not in this space — it belongs to Property 3's valid space).
_NON_OBJECT_OVERRIDES = st.sampled_from([
    "dent: small surface dent",
    42,
    2.5,
    True,
    False,
    ["dent"],
    [{"label": "dent", "prompt": "a small dent"}],
])

# Non-string values for an in-Label_Set key.
_NON_STRING_VALUES = st.sampled_from([
    None, 7, 2.5, True, False, ["a prompt"], {"prompt": "a dent"},
])


@st.composite
def _valid_grounded_sam_cases(draw):
    """One valid grounded-sam submission: a geometry modality, a valid
    Label_Set, and an override state — absent (None) or a map over
    in-Label_Set keys with values mixing blank / non-blank / unicode /
    boundary-length strings (including the present-but-empty map)."""
    modality = draw(st.sampled_from(GEOMETRY_MODALITIES))
    labels = draw(_label_sets)
    overrides = None
    if draw(st.booleans()):
        keys = draw(st.lists(st.sampled_from(labels), unique=True,
                             max_size=len(labels)))
        overrides = {key: draw(_override_values) for key in keys}
    return SimpleNamespace(modality=modality, labels=labels,
                           overrides=overrides)


@st.composite
def _malformed_override_cases(draw):
    """One malformed grounded-sam submission: a non-object
    prompt_overrides, a key outside the Label_Set (including
    whitespace-padded spellings of real labels — override keys are not
    stripped), a non-string value, or a value of raw length 257. The
    offense may sit beside otherwise-valid entries."""
    modality = draw(st.sampled_from(GEOMETRY_MODALITIES))
    labels = draw(_label_sets)
    kind = draw(st.sampled_from(
        ("non_object", "unknown_key", "non_string_value", "over_length")))
    if kind == "non_object":
        return SimpleNamespace(modality=modality, labels=labels, kind=kind,
                               offender=None,
                               overrides=draw(_NON_OBJECT_OVERRIDES))

    base_pool = list(labels)
    if kind == "unknown_key":
        offender = draw(st.one_of(
            st.sampled_from(["", "  "]),
            st.builds(lambda label: f" {label} ", st.sampled_from(labels)),
            st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=64),
        ).filter(lambda key: key not in labels))
        value = draw(_override_values)
    else:
        offender = draw(st.sampled_from(labels))
        base_pool.remove(offender)
        if kind == "non_string_value":
            value = draw(_NON_STRING_VALUES)
        else:  # over_length: raw length 257, the first rejected length
            value = draw(st.text(alphabet=_TEXT_ALPHABET,
                                 min_size=OVERRIDE_LENGTH_LIMIT + 1,
                                 max_size=OVERRIDE_LENGTH_LIMIT + 1))

    overrides = {}
    if base_pool:
        for key in draw(st.lists(st.sampled_from(base_pool), unique=True,
                                 max_size=len(base_pool))):
            overrides[key] = draw(_override_values)
    overrides[offender] = value
    return SimpleNamespace(modality=modality, labels=labels, kind=kind,
                           offender=offender, overrides=overrides)


# Safe identifier characters for llm:/bedrock: model ids (no whitespace,
# no control characters; colons are legitimate — 'us.amazon.nova-pro-v1:0').
_IDENTIFIER_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789.:-"

_llm_identifiers = st.one_of(
    st.sampled_from(["us.amazon.nova-pro-v1:0",
                     "anthropic.claude-3-5-sonnet-20240620-v1:0"]),
    st.text(alphabet=_IDENTIFIER_ALPHABET, min_size=1, max_size=40),
)

_bedrock_suffixes = st.one_of(
    st.sampled_from(["anthropic.claude-3-haiku", "us.amazon.nova-lite-v1:0"]),
    st.text(alphabet=_IDENTIFIER_ALPHABET, min_size=1, max_size=40),
)

_detection_prompts = st.one_of(
    st.just('  Find every "defect" {and mark it}\n — done  '),
    st.text(alphabet=_TEXT_ALPHABET, min_size=1,
            max_size=100).filter(lambda t: t.strip()),
)

# A stray prompt_overrides value planted on a non-grounded-sam
# submission: ignored by the pre-feature creation rules, so it must never
# reach the record post-feature either (None = nothing planted).
_planted_overrides = st.one_of(
    st.none(),
    st.just({}),
    st.dictionaries(
        st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=20),
        st.text(alphabet=_TEXT_ALPHABET, max_size=30),
        max_size=2),
    st.just("planted-garbage"),
)


@st.composite
def _other_family_cases(draw):
    """One valid submission of an existing family: sam / bedrock: / llm:
    with a modality from that family's pre-feature matrix, with and
    without skip-verification, optionally carrying a planted stray
    prompt_overrides value."""
    family = draw(st.sampled_from(("sam", "bedrock", "llm")))
    if family == "sam":
        model = "sam"
        modality = draw(st.sampled_from(("Segmentation", "ObjectDetection")))
    elif family == "bedrock":
        model = "bedrock:" + draw(_bedrock_suffixes)
        modality = draw(st.sampled_from(("Classification",
                                         "ObjectDetection")))
    else:
        model = "llm:" + draw(_llm_identifiers)
        modality = draw(st.sampled_from(("Classification", "Segmentation",
                                         "ObjectDetection")))
    labels = (None if modality == "Classification" else draw(_label_sets))
    return SimpleNamespace(
        family=family,
        model=model,
        modality=modality,
        labels=labels,
        detection_prompt=(draw(_detection_prompts) if family == "llm"
                          else None),
        skip=draw(st.booleans()),
        planted=draw(_planted_overrides),
    )


# ----------------------------------------------------------------- helpers

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


# =========================================================================== #
# Property 3: Job creation persists the model and the normalized overrides
# =========================================================================== #

class TestProperty3PersistsModelAndNormalizedOverrides:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_valid_grounded_sam_cases())
    @example(case=SimpleNamespace(          # no overrides key at all
        modality="ObjectDetection", labels=["fod"], overrides=None))
    @example(case=SimpleNamespace(          # 256-boundary kept, blank dropped
        modality="Segmentation", labels=["dent", "scratch"],
        overrides={"dent": "x" * OVERRIDE_LENGTH_LIMIT, "scratch": " \t "}))
    @example(case=SimpleNamespace(          # unicode at the 256 boundary
        modality="Segmentation", labels=["\u0434\u0435\u0444\u0435\u043a\u0442"],
        overrides={"\u0434\u0435\u0444\u0435\u043a\u0442":
                   "\u00fc" * OVERRIDE_LENGTH_LIMIT}))
    def test_property_grounded_sam_record_carries_normalized_overrides(
            self, aws_stack, dda, case):
        """Feature: grounded-sam-autolabel, Property 3: Job creation
        persists the model and the normalized overrides — *For any* valid
        `grounded-sam` submission (Segmentation or ObjectDetection, any
        Label_Set, any override map of in-Label_Set keys with string values
        <= 256 characters mixing blank and non-blank), the created job
        record SHALL carry `auto_label.model == 'grounded-sam'` and
        `auto_label.prompt_overrides` equal to exactly the
        non-blank-after-trim entries character-for-character, with the key
        absent when none survives.

        **Validates: Requirements 1.5, 2.4**
        """
        env = CreateJobEnv(aws_stack, dda)
        env.put_images(["a.jpg"])

        auto_label = {"enabled": True, "model": "grounded-sam"}
        if case.overrides is not None:
            auto_label["prompt_overrides"] = dict(case.overrides)

        status, response_body = env.create(
            task_type=case.modality, label_set=list(case.labels),
            auto_label=auto_label)
        assert status == 201, (
            f"valid grounded-sam submission rejected "
            f"(overrides={case.overrides!r}): {response_body!r}")

        # The normalization oracle, restated from Req 2.4: survivors are
        # exactly the entries non-empty after trimming, values kept
        # character-for-character; the key is absent when none survives.
        survivors = {key: value
                     for key, value in (case.overrides or {}).items()
                     if value.strip()}
        expected = {"enabled": True, "model": "grounded-sam"}
        if survivors:
            expected["prompt_overrides"] = survivors

        job = env.get_job(response_body["job_id"])
        assert job["auto_label"] == expected, (
            f"auto_label drifted from the normalization oracle for "
            f"overrides={case.overrides!r}: {job['auto_label']!r}")


# =========================================================================== #
# Property 4: Malformed overrides are rejected and nothing persists
# =========================================================================== #

class TestProperty4MalformedOverridesRejected:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_malformed_override_cases())
    @example(case=SimpleNamespace(          # 257 is the first rejected length
        modality="ObjectDetection", labels=["dent"], kind="over_length",
        offender="dent",
        overrides={"dent": "x" * (OVERRIDE_LENGTH_LIMIT + 1)}))
    @example(case=SimpleNamespace(          # a non-object overrides value
        modality="Segmentation", labels=["dent"], kind="non_object",
        offender=None, overrides=["dent"]))
    def test_property_malformed_overrides_rejected_nothing_persisted(
            self, aws_stack, dda, case):
        """Feature: grounded-sam-autolabel, Property 4: Malformed overrides
        are rejected and nothing persists — *For any* `grounded-sam`
        submission whose `prompt_overrides` is not an object of string
        values, carries a key outside the submitted Label_Set, or carries a
        value whose raw length exceeds 256 characters, the creation request
        SHALL be rejected with a validation error identifying the offense,
        and no job record SHALL be persisted.

        **Validates: Requirements 2.5, 2.6**
        """
        env = CreateJobEnv(aws_stack, dda)
        env.put_images(["a.jpg"])

        status, response_body = env.create(
            task_type=case.modality, label_set=list(case.labels),
            auto_label={"enabled": True, "model": "grounded-sam",
                        "prompt_overrides": case.overrides})
        assert status == 400, (
            f"malformed prompt_overrides accepted "
            f"({case.kind}): {case.overrides!r}")

        errors = [error for error in response_body["validation_errors"]
                  if error["parameter"] == "auto_label"]
        if case.kind == "non_object":
            assert any("prompt_overrides must be an object"
                       in error["message"] for error in errors), (
                f"non-object offense not identified: {errors!r}")
        elif case.kind == "unknown_key":
            assert any(error.get("label") == case.offender
                       and "is not a label" in error["message"]
                       for error in errors), (
                f"unknown key {case.offender!r} not identified: {errors!r}")
        elif case.kind == "non_string_value":
            assert any(error.get("label") == case.offender
                       and "must be text" in error["message"]
                       for error in errors), (
                f"non-string value for {case.offender!r} not identified: "
                f"{errors!r}")
        else:  # over_length
            assert any(error.get("label") == case.offender
                       and f"at most {OVERRIDE_LENGTH_LIMIT} characters"
                       in error["message"]
                       for error in errors), (
                f"over-length value for {case.offender!r} not identified: "
                f"{errors!r}")

        env.assert_nothing_persisted()


# =========================================================================== #
# Property 15: Other families' job records are byte-identical to
# pre-feature records
# =========================================================================== #

class TestProperty15OtherFamiliesPreFeatureRecords:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_other_family_cases())
    @example(case=SimpleNamespace(          # planted overrides on llm + skip
        family="llm", model="llm:us.amazon.nova-pro-v1:0",
        modality="Classification", labels=None,
        detection_prompt="Find every defect", skip=True,
        planted={"normal": "planted override"}))
    @example(case=SimpleNamespace(          # planted non-object on sam
        family="sam", model="sam", modality="Segmentation",
        labels=["dent"], detection_prompt=None, skip=False,
        planted="planted-garbage"))
    def test_property_other_family_records_match_pre_feature_shape(
            self, aws_stack, dda, case):
        """Feature: grounded-sam-autolabel, Property 15: Other families'
        job records are byte-identical to pre-feature records — *For any*
        valid submission of the `sam`, `bedrock:` or `llm:` family (with
        and without skip-verification), the created job record SHALL
        contain no `prompt_overrides` key anywhere and SHALL equal, key for
        key, the record the pre-feature creation rules produce for the same
        submission.

        **Validates: Requirements 2.8, 7.1**
        """
        env = CreateJobEnv(aws_stack, dda)
        env.put_images(["a.jpg"])
        job_name = f"job-{uuid.uuid4().hex[:12]}"

        auto_label = {"enabled": True, "model": case.model}
        if case.family == "llm":
            auto_label["detection_prompt"] = case.detection_prompt
        if case.planted is not None:
            auto_label["prompt_overrides"] = case.planted

        submission = dict(job_name=job_name, task_type=case.modality,
                          label_set=(list(case.labels) if case.labels
                                     else None),
                          auto_label=auto_label)
        if case.skip:
            user = env.make_user(role="PortalAdmin")
            effective_labels = (CLASSIFICATION_LABELS
                                if case.modality == "Classification"
                                else case.labels)
            per_label_prompts = {label: f"Is '{label}' visible?"
                                 for label in effective_labels}
            submission.update(team_id=None, skip_verification=True,
                              bedrock_model_id=BEDROCK_SKIP_MODEL,
                              per_label_prompts=per_label_prompts)
        else:
            user = env.creator
            per_label_prompts = None

        status, response_body = env.create(user=user, **submission)
        assert status == 201, (
            f"valid {case.family} submission rejected: {response_body!r}")
        job = env.get_job(response_body["job_id"])

        # Req 2.8: no prompt_overrides key anywhere in the record — even
        # when the submission planted one.
        assert not _contains_key_anywhere(job, "prompt_overrides"), (
            f"prompt_overrides leaked into a {case.family} record "
            f"(planted={case.planted!r}): {job!r}")

        # Req 7.1: the pre-feature top-level key set, exactly.
        expected_keys = set(PRE_FEATURE_RECORD_KEYS)
        expected_keys |= ({"bedrock_model_id", "per_label_prompts"}
                          if case.skip else {"team_id"})
        assert set(job.keys()) == expected_keys, (
            f"record keys drifted from the pre-feature shape: "
            f"{sorted(set(job.keys()) ^ expected_keys)!r}")

        # The per-family pre-feature auto_label document, restated from
        # the design's data model: {enabled, model} plus detection_prompt
        # for llm: only — never few_shot/sizing keys (none submitted),
        # never prompt_overrides.
        expected_auto_label = {"enabled": True, "model": case.model}
        if case.family == "llm":
            expected_auto_label["detection_prompt"] = case.detection_prompt
        assert job["auto_label"] == expected_auto_label, (
            f"auto_label drifted from the pre-feature document: "
            f"{job['auto_label']!r}")

        # Every other submission-derived field carries the pre-feature
        # value.
        expected_label_set = (list(CLASSIFICATION_LABELS)
                              if case.modality == "Classification"
                              else list(case.labels))
        assert job["job_id"] == response_body["job_id"]
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
        assert job["skip_verification"] is case.skip
        assert job["submitted_count"] == 0
        assert job["blocked"] is False
        assert job["created_by"] == user["user_id"]
        if case.skip:
            assert job["bedrock_model_id"] == BEDROCK_SKIP_MODEL
            assert job["per_label_prompts"] == per_label_prompts
        else:
            assert job["team_id"] == env.team_id
