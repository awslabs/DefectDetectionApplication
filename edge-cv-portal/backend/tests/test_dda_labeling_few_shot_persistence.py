"""
Few_Shot_Option persistence in create_dda_job (dda_labeling.py)
(llm-autolabel-prompt-tuning, task 5.2).

Feature: llm-autolabel-prompt-tuning

Covers, against the moto-backed stack from conftest.py (real
shared_utils / rbac path, moto DynamoDB + S3, fake Cognito for member
role resolution — the test_dda_labeling_create_job.py convention),
calling `create_dda_job(body, user)` directly:

- Req 6.4: an enabled Few_Shot_Option persists `auto_label.few_shot`
  with every submitted example reference carrying its good-or-bad
  designation and its position in stored order, so the submitted
  example set and its order are recoverable exactly (the persistence
  half of Property 21)
- Req 10.6: an `llm:` job with the option off persists
  `few_shot = {"enabled": false}`
- Req 10.1: no `few_shot` key at all on `sam` / `bedrock:` jobs, even
  when the submission carries the option
- Req 10.4: a submission that omits the field is accepted with the
  pre-feature record shape (no `few_shot` key)
- Req 6.2/6.3: the option enabled with zero example images is rejected
  with a validation error naming `few_shot`, persisting nothing
- Req 10.6: `example_images` keeps its labeler-instruction role
  untouched (byte-identical to the submission) in every case
"""
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest

from test_dda_labeling_create_job import (
    DATASET_BUCKET,
    POOL_ID,
    REGION,
    CreateJobEnv,
    FakeCognitoClient,
    FakeLambdaClient,
    LLM_MODEL,
    llm_auto_label,
    messages,
)

SAM_MODEL = "sam"
BEDROCK_MODEL = "bedrock:anthropic.claude-3-haiku"


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock, with
    fake Cognito and Lambda clients, plus the dataset bucket."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling

    fake_cognito = FakeCognitoClient()
    dda_labeling.cognito_client = fake_cognito
    dda_labeling.USER_POOL_ID = POOL_ID

    fake_lambda = FakeLambdaClient()
    dda_labeling.lambda_client = fake_lambda

    # Idempotent in moto's us-east-1 (BucketAlreadyOwnedByYou is a 200).
    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket=DATASET_BUCKET)

    return SimpleNamespace(module=dda_labeling, cognito=fake_cognito,
                           lambda_client=fake_lambda)


@pytest.fixture
def env(aws_stack, dda):
    return CreateJobEnv(aws_stack, dda)


# ------------------------------------------------------------- helpers

def example_refs(good_count, bad_count):
    """Distinct, order-revealing example references per designation."""
    return {
        "good": [f"ex/good-{i}-{uuid.uuid4().hex[:6]}.jpg"
                 for i in range(good_count)],
        "bad": [f"ex/bad-{i}-{uuid.uuid4().hex[:6]}.png"
                for i in range(bad_count)],
    }


def expected_examples(refs):
    """The `few_shot.examples` document Req 6.4 requires for `refs`:
    good in stored order first, then bad, each with its designation and
    its position within that designation."""
    return [{"ref": ref, "designation": designation, "position": position}
            for designation in ("good", "bad")
            for position, ref in enumerate(refs[designation])]


def create_llm_job(env, refs=None, few_shot=None, top_level=False,
                   **overrides):
    """Create an `llm:` job, optionally carrying a Few_Shot_Option value
    nested under auto_label (the wizard's shape) or at the top level."""
    auto_label = llm_auto_label()
    if few_shot is not None and not top_level:
        auto_label["few_shot"] = few_shot
    if few_shot is not None and top_level:
        overrides["few_shot"] = few_shot
    if refs is not None:
        overrides["example_images"] = refs
    env.put_images(["a.jpg"])
    return env.create(auto_label=auto_label, **overrides)


# ------------------------------------- enabled option: set and order

class TestEnabledPersistsSetAndOrder:
    @pytest.mark.parametrize("good_count,bad_count", [
        (1, 0), (0, 1), (2, 3), (10, 10),
    ])
    def test_designations_and_positions_recover_submission(
            self, env, good_count, bad_count):
        """Req 6.4 (Property 21, persistence half): every submitted
        example reference is persisted with its good-or-bad designation
        and its position in stored order, so the submitted set and order
        are recoverable exactly."""
        refs = example_refs(good_count, bad_count)
        status, body = create_llm_job(env, refs=refs,
                                      few_shot={"enabled": True})
        assert status == 201

        few_shot = env.get_job(body["job_id"])["auto_label"]["few_shot"]
        assert few_shot["enabled"] is True
        assert few_shot["examples"] == expected_examples(refs)

        # The submitted set and order are recoverable from the record
        # alone, independent of list ordering in storage.
        recovered = {"good": [], "bad": []}
        for example in sorted(few_shot["examples"],
                              key=lambda e: int(e["position"])):
            recovered[example["designation"]].append(example["ref"])
        assert recovered == refs

    def test_enabled_leaves_rest_of_auto_label_intact(self, env):
        """Req 6.4: `few_shot` is additive — the model identifier and the
        raw Detection_Prompt are persisted exactly as before."""
        refs = example_refs(1, 1)
        status, body = create_llm_job(env, refs=refs,
                                      few_shot={"enabled": True})
        assert status == 201
        auto_label = env.get_job(body["job_id"])["auto_label"]
        assert auto_label["enabled"] is True
        assert auto_label["model"] == LLM_MODEL
        assert auto_label["detection_prompt"] == (
            "Find every visible surface defect")
        assert set(auto_label) == {"enabled", "model", "detection_prompt",
                                   "few_shot"}

    def test_top_level_option_persists_identically(self, env):
        """The option is accepted at the top level of the submission as
        well as nested under auto_label, persisting the same document."""
        refs = example_refs(2, 1)
        status, body = create_llm_job(env, refs=refs,
                                      few_shot={"enabled": True},
                                      top_level=True)
        assert status == 201
        assert env.get_job(body["job_id"])["auto_label"]["few_shot"] == {
            "enabled": True, "examples": expected_examples(refs)}


# --------------------------------------------- option off / omitted

class TestOptionOffAndOmitted:
    @pytest.mark.parametrize("submitted", [{"enabled": False}, False])
    def test_disabled_persists_enabled_false(self, env, submitted):
        """Req 10.6: an `llm:` job with the option off persists the
        Few_Shot_Option as disabled and carries no example set."""
        refs = example_refs(2, 2)
        status, body = create_llm_job(env, refs=refs, few_shot=submitted)
        assert status == 201
        assert env.get_job(body["job_id"])["auto_label"]["few_shot"] == {
            "enabled": False}

    def test_omitted_field_accepted_with_no_few_shot_key(self, env):
        """Req 10.4: a submission that omits the Few_Shot_Option is
        accepted under the pre-feature validation rules and produces the
        pre-feature record — no `few_shot` key at all."""
        refs = example_refs(1, 1)
        status, body = create_llm_job(env, refs=refs)
        assert status == 201
        auto_label = env.get_job(body["job_id"])["auto_label"]
        assert "few_shot" not in auto_label
        assert auto_label == {
            "enabled": True, "model": LLM_MODEL,
            "detection_prompt": "Find every visible surface defect"}

    def test_omitted_field_with_no_example_images_accepted(self, env):
        """Req 10.4: omitting the option is never a rejection, not even
        when the job has no example images at all."""
        status, body = create_llm_job(env)
        assert status == 201
        assert "few_shot" not in env.get_job(body["job_id"])["auto_label"]


# ----------------------------------------- other model families (10.1)

class TestOtherModelFamiliesCarryNoKey:
    @pytest.mark.parametrize("model,task_type,label_set", [
        (SAM_MODEL, "Segmentation", ["scratch"]),
        (BEDROCK_MODEL, "Classification", None),
    ])
    @pytest.mark.parametrize("few_shot", [None, {"enabled": True},
                                          {"enabled": False}])
    def test_no_few_shot_key_for_sam_or_bedrock(
            self, env, model, task_type, label_set, few_shot):
        """Req 10.1: `sam` / `bedrock:` jobs get no `few_shot` key and no
        example set, whatever the submission carries."""
        refs = example_refs(2, 2)
        auto_label = {"enabled": True, "model": model}
        if few_shot is not None:
            auto_label["few_shot"] = few_shot
        env.put_images(["a.jpg"])
        status, body = env.create(task_type=task_type, label_set=label_set,
                                  example_images=refs,
                                  auto_label=auto_label)
        assert status == 201
        assert env.get_job(body["job_id"])["auto_label"] == {
            "enabled": True, "model": model}

    def test_no_auto_label_job_carries_no_few_shot_key(self, env):
        """Req 10.1: a job with auto-labeling off is untouched too."""
        env.put_images(["a.jpg"])
        status, body = env.create(example_images=example_refs(1, 1),
                                  few_shot={"enabled": True})
        assert status == 201
        assert env.get_job(body["job_id"])["auto_label"] == {"enabled": False}


# ------------------------------------- enabled with zero examples (6.2/6.3)

class TestEnabledWithoutExamplesRejected:
    @pytest.mark.parametrize("refs", [
        None,
        {"good": [], "bad": []},
    ])
    def test_rejected_naming_few_shot_persisting_nothing(self, env, refs):
        """Req 6.2/6.3: the option enabled with zero example images is
        rejected with a validation error naming `few_shot`, and no job or
        task item is persisted."""
        status, body = create_llm_job(env, refs=refs,
                                      few_shot={"enabled": True})
        assert status == 400
        offenders = [err for err in body["validation_errors"]
                     if err["parameter"] == "few_shot"]
        assert len(offenders) == 1
        assert "at least one example image is required" in (
            offenders[0]["message"].lower())
        env.assert_nothing_persisted()

    def test_malformed_option_value_rejected_naming_few_shot(self, env):
        """A non-object, non-boolean option value is a validation error on
        `few_shot`, not a silently persisted document."""
        status, body = create_llm_job(env, refs=example_refs(1, 1),
                                      few_shot="yes please")
        assert status == 400
        assert "few_shot" in {err["parameter"]
                              for err in body["validation_errors"]}
        env.assert_nothing_persisted()

    def test_rejection_enumerated_with_other_violations(self, env):
        """Req 6.3 joins the shared enumerated error list rather than
        short-circuiting it."""
        env.put_images(["a.jpg"])
        status, body = env.create(
            job_name="", auto_label={**llm_auto_label(),
                                     "few_shot": {"enabled": True}})
        assert status == 400
        parameters = {err["parameter"] for err in body["validation_errors"]}
        assert {"job_name", "few_shot"} <= parameters
        assert "between 1 and 63" in messages(body)
        env.assert_nothing_persisted()


# ----------------------------------- example_images left untouched (10.6)

class TestExampleImagesUntouched:
    @pytest.mark.parametrize("few_shot", [
        None, {"enabled": True}, {"enabled": False},
    ])
    def test_example_images_persisted_unchanged_for_llm(self, env, few_shot):
        """Req 10.6: whatever the Few_Shot_Option, `example_images` keeps
        its existing labeler-instruction role — persisted exactly as
        submitted, never reordered or annotated."""
        refs = example_refs(3, 2)
        submitted = {"good": list(refs["good"]), "bad": list(refs["bad"])}
        status, body = create_llm_job(env, refs=refs, few_shot=few_shot)
        assert status == 201
        assert env.get_job(body["job_id"])["example_images"] == submitted

    def test_example_images_unchanged_for_sam(self, env):
        refs = example_refs(1, 2)
        env.put_images(["a.png"])
        status, body = env.create(
            task_type="Segmentation", label_set=["scratch"],
            example_images=refs,
            auto_label={"enabled": True, "model": SAM_MODEL,
                        "few_shot": {"enabled": True}})
        assert status == 201
        assert env.get_job(body["job_id"])["example_images"] == refs
