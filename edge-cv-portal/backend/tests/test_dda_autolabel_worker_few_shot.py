"""
dda_autolabel_worker._resolve_few_shot_images — Few_Shot_Example
resolution at labeling time.

Spec: llm-autolabel-prompt-tuning, task 4.2 (example-based unit tests).

Covers, against the moto-backed stack from conftest.py and the
`AutolabelEnv` harness from test_dda_autolabel_worker.py, with every
`get_s3_client_for_bucket` call and every `get_object` recorded:

- **Disabled by default** (Req 10.3): an absent `auto_label`, a `null`
  `auto_label`, and an absent / `null` / non-dict `few_shot` document,
  an `enabled`-falsy document, a non-list / empty `examples` list, and a
  list whose entries are all malformed (not a dict, or a missing / blank
  / non-string `ref`) each resolve to no attachments, the resolved
  Model_Image_Limit, zero S3 clients and zero `get_object` calls — and
  never a failure. Only `enabled is True` with at least one well-formed
  example attaches anything.
- **Attachment order and bounds** (Req 6.5, 7.3, 7.4): good examples in
  stored order first, then bad examples in stored order, each image
  carrying its stored `designation` unchanged and the Converse format
  derived from the ref's extension; omitted refs assert **zero**
  `get_object` calls; `limit == 1` attaches and reads nothing; malformed
  entries are dropped before selection so they never consume a slot; one
  S3 client is obtained per bucket.
- **Unreadable example** (Req 6.7): the reason text names the ref
  verbatim — `few-shot example image {ref} is not accessible: {cause}`,
  and the unresolvable-reference variant — and fails only that dataset
  image: driven through the real SQS handler with a multi-record batch,
  the broken job's task is Failed with no model invocation while a
  few-shot-enabled job and a few-shot-absent job in the same batch both
  reach `Available`, with no batch item failures.

Requirements: 6.5, 6.7, 7.3, 7.4, 10.3

Spec: llm-model-token-and-image-sizing, task 8.2 (job-record sizing
reads). Extends the same harness — every pre-existing case above holds
unchanged, since Few_Shot_Example selection is independent of the
Downscale_Setting (Req 8.3):

- **Reading both values off the job record** (Req 5.8, 8.1): a persisted
  `auto_label.downscale_max_edge` downscales the target and every
  attached example with that one setting (DynamoDB Decimals converted
  before the resolvers see them), and a persisted valid
  `auto_label.token_budget` is the request's `maxTokens`, unchanged by
  whatever the Model_Token_Limits mapping says today.
- **Malformed values are never failures** (Req 3.8, 5.9, 5.12, 10.10): a
  malformed, null or absent `downscale_max_edge` is Downscale_Off — the
  source bytes pass through byte-identically, proven with undecodable
  header-only bytes that any wrongly-applied bound would refuse — and a
  malformed or absent `token_budget` falls through to the mapping and
  then the default of 10000.
- **A refused example fails only its own dataset image** (Req 8.5, 9.2):
  driven through the real SQS handler with a multi-record batch, an
  example the Image_Downscaler cannot decode for the job's setting marks
  that task Failed with a reason naming the example and the requested
  bound, invokes no model for it, and leaves the sibling records'
  outcomes untouched, with no batch item failures.

Requirements: 3.8, 5.8, 5.9, 5.12, 8.1, 8.5, 9.2, 10.10
"""
import io
import json
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import boto3
import pytest

from dda_llm_request import FEW_SHOT_HEADER, FEW_SHOT_TARGET_INTRO
from test_dda_autolabel_worker import (
    AutolabelEnv,
    DATASET_BUCKET,
    SAM_FUNCTION,
    jpeg_bytes,
    png_bytes,
)

REGION = "us-east-1"
ARTIFACTS_BUCKET = "test-portal-artifacts"

MODEL_ID = "us.amazon.nova-pro-v1:0"
MODEL = f"llm:{MODEL_ID}"
LABELS = ["scratch", "dent"]
PROMPT = '  Find every "scratch" {and dent}\n  on the panel.  '
WIDTH, HEIGHT = 100, 80

# The shared-layer default Model_Image_Limit (Req 7.1): every test that
# does not configure LLM_MODEL_IMAGE_LIMITS must resolve this.
DEFAULT_LIMIT = 20

GOOD = "good"
BAD = "bad"

# Sentinels: "this key is not present in the document at all" (_ABSENT
# for `few_shot`, _OMIT for `auto_label` itself).
_ABSENT = object()
_OMIT = object()

BOX = {"class": "scratch",
       "box": {"left": 10, "top": 5, "width": 30, "height": 20}}


def guidance(detections):
    return json.dumps({"detections": detections})


# ------------------------------------------------------------------- harness

class _RecordingS3:
    """S3 client proxy recording every (Bucket, Key) read."""

    def __init__(self, inner, calls):
        self._inner = inner
        self._calls = calls

    def get_object(self, **kwargs):
        self._calls.append((kwargs.get("Bucket"), kwargs.get("Key")))
        return self._inner.get_object(**kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class FewShotEnv(AutolabelEnv):
    """AutolabelEnv plus few-shot fixtures: example uploads, job
    documents, and a recording S3 seam."""

    def __init__(self, stack, worker, monkeypatch):
        super().__init__(stack, worker, monkeypatch)
        self.get_object_calls = []
        self.client_requests = []
        self.example_bytes = {}

        real_factory = worker.get_s3_client_for_bucket
        calls, requests = self.get_object_calls, self.client_requests

        def recording_factory(usecase, bucket,
                              session_name="portal-s3-access"):
            requests.append(bucket)
            return _RecordingS3(real_factory(usecase, bucket, session_name),
                                calls)

        monkeypatch.setattr(worker, "get_s3_client_for_bucket",
                            recording_factory)

    # ------------------------------------------------------------ examples
    def put_example(self, designation, position, ext="png",
                    bucket=ARTIFACTS_BUCKET, width=20, height=10):
        """Upload one example image and return its stored reference."""
        key = (f"labeling-examples/{uuid.uuid4().hex[:8]}/{designation}/"
               f"{position}.{ext}")
        body = (png_bytes(width, height) if ext.lower() == "png"
                else jpeg_bytes(width, height))
        self.s3.put_object(Bucket=bucket, Key=key, Body=body)
        # Portal artifacts refs are bare keys (the wizard's presigned-PUT
        # uploads); other buckets are stored as full s3:// URIs.
        ref = key if bucket == ARTIFACTS_BUCKET else f"s3://{bucket}/{key}"
        self.example_bytes[ref] = body
        return {"ref": ref, "designation": designation, "position": position}

    def missing_example(self, designation=GOOD, position=0):
        return {"ref": f"labeling-examples/{uuid.uuid4().hex[:8]}/nope.png",
                "designation": designation, "position": position}

    # ------------------------------------------------------- job documents
    def job_doc(self, few_shot=_ABSENT, auto_label=_ABSENT):
        """A job record as the worker reads it (no DynamoDB needed for
        the direct resolver calls)."""
        job = {"job_id": "job-direct", "usecase_id": self.usecase_id}
        if auto_label is not _ABSENT:
            if auto_label is not _OMIT:
                job["auto_label"] = auto_label
            return job
        document = {"enabled": True, "model": MODEL}
        if few_shot is not _ABSENT:
            document["few_shot"] = few_shot
        job["auto_label"] = document
        return job

    def resolve(self, few_shot=_ABSENT, auto_label=_ABSENT,
                model_identifier=MODEL_ID):
        return self.worker._resolve_few_shot_images(
            self.job_doc(few_shot=few_shot, auto_label=auto_label),
            model_identifier)

    def set_image_limit(self, limit, model_identifier=MODEL_ID):
        self.monkeypatch.setenv("LLM_MODEL_IMAGE_LIMITS",
                                json.dumps({model_identifier: limit}))

    # --------------------------------------------------------- assertions
    def read_keys(self):
        return [key for _bucket, key in self.get_object_calls]

    def make_llm_job(self, few_shot=_ABSENT, task_type="ObjectDetection"):
        """A persisted `llm:` job, optionally carrying a few_shot doc."""
        job_id = self.make_job(task_type=task_type, label_set=LABELS,
                               model=MODEL)
        if few_shot is not _ABSENT:
            self.stack.tables.labeling_jobs.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET auto_label.few_shot = :fs",
                ExpressionAttributeValues={":fs": few_shot})
        return job_id


@pytest.fixture(scope="module")
def worker(aws_stack):
    """The real dda_autolabel_worker imported inside the moto mock."""
    os.environ["SAM_WORKER_FUNCTION_NAME"] = SAM_FUNCTION
    sys.modules.pop("dda_autolabel_worker", None)
    import dda_autolabel_worker

    dda_autolabel_worker.SAM_WORKER_FUNCTION_NAME = SAM_FUNCTION
    dda_autolabel_worker.PORTAL_ARTIFACTS_BUCKET = ARTIFACTS_BUCKET
    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=DATASET_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    return dda_autolabel_worker


@pytest.fixture
def env(aws_stack, worker, monkeypatch):
    monkeypatch.delenv("LLM_MODEL_IMAGE_LIMITS", raising=False)
    return FewShotEnv(aws_stack, worker, monkeypatch)


# ----------------------------------------------------- disabled by default

class TestDisabledByDefault:
    """Req 10.3: an absent or malformed few-shot document is disabled —
    no attachment, no S3 access, no failure."""

    @pytest.mark.parametrize("few_shot", [
        pytest.param(_ABSENT, id="few_shot-absent"),
        pytest.param(None, id="few_shot-null"),
        pytest.param("enabled", id="few_shot-string"),
        pytest.param(["good.png"], id="few_shot-list"),
        pytest.param(42, id="few_shot-number"),
        pytest.param({}, id="enabled-absent"),
        pytest.param({"enabled": False}, id="enabled-false"),
        pytest.param({"enabled": 0}, id="enabled-zero"),
        pytest.param({"enabled": ""}, id="enabled-blank"),
        pytest.param({"enabled": None}, id="enabled-null"),
        pytest.param({"enabled": True}, id="examples-absent"),
        pytest.param({"enabled": True, "examples": None},
                     id="examples-null"),
        pytest.param({"enabled": True, "examples": "a.png"},
                     id="examples-string"),
        pytest.param({"enabled": True, "examples": {"ref": "a.png"}},
                     id="examples-dict"),
        pytest.param({"enabled": True, "examples": []}, id="examples-empty"),
        pytest.param({"enabled": True, "examples": ["a.png", None, 7]},
                     id="examples-not-dicts"),
        pytest.param({"enabled": True,
                      "examples": [{"designation": GOOD, "position": 0}]},
                     id="examples-ref-missing"),
        pytest.param({"enabled": True, "examples": [{"ref": ""}]},
                     id="examples-ref-blank"),
        pytest.param({"enabled": True, "examples": [{"ref": None}]},
                     id="examples-ref-null"),
        pytest.param({"enabled": True, "examples": [{"ref": 12}]},
                     id="examples-ref-non-string"),
    ])
    def test_document_resolves_disabled_without_reading_anything(self, env,
                                                                few_shot):
        images, limit = env.resolve(few_shot=few_shot)

        assert images == []
        assert limit == DEFAULT_LIMIT
        assert env.client_requests == []
        assert env.get_object_calls == []

    @pytest.mark.parametrize("auto_label", [
        pytest.param(_OMIT, id="auto_label-absent"),
        pytest.param(None, id="auto_label-null"),
    ])
    def test_absent_or_null_auto_label_resolves_disabled(self, env,
                                                         auto_label):
        images, limit = env.resolve(auto_label=auto_label)

        assert images == []
        assert limit == DEFAULT_LIMIT
        assert env.get_object_calls == []

    def test_enabled_true_with_one_well_formed_example_attaches(self, env):
        """The only enabling shape: `enabled is True` plus at least one
        well-formed reference."""
        example = env.put_example(GOOD, 0)

        images, limit = env.resolve(
            few_shot={"enabled": True, "examples": [example]})

        assert limit == DEFAULT_LIMIT
        assert [image["designation"] for image in images] == [GOOD]
        assert images[0]["bytes"] == env.example_bytes[example["ref"]]
        assert env.read_keys() == [example["ref"]]

    def test_disabled_document_still_resolves_the_configured_limit(self, env):
        """The bound is resolved even with the option off, so the
        request the worker builds is bounded either way."""
        env.set_image_limit(4)

        images, limit = env.resolve(few_shot={"enabled": False})

        assert images == []
        assert limit == 4


# ------------------------------------------------- attachment and ordering

class TestAttachmentOrderAndBounds:
    """Req 6.5, 7.3, 7.4: good-then-bad stored order, each example
    identified, omitted refs never read."""

    def test_good_then_bad_in_stored_order_with_designations(self, env):
        """Storage order interleaves the designations; attachment order
        is every good example first, then every bad one, each carrying
        its stored designation unchanged."""
        bad_0 = env.put_example(BAD, 0, ext="jpg")
        good_0 = env.put_example(GOOD, 0, ext="png")
        bad_1 = env.put_example(BAD, 1, ext="jpeg")
        good_1 = env.put_example(GOOD, 1, ext="PNG")
        stored = [bad_0, good_0, bad_1, good_1]

        images, limit = env.resolve(
            few_shot={"enabled": True, "examples": stored})

        assert limit == DEFAULT_LIMIT
        assert [image["designation"] for image in images] == [
            GOOD, GOOD, BAD, BAD]
        # Formats derive from the ref's extension (case-insensitive).
        assert [image["format"] for image in images] == [
            "png", "png", "jpeg", "jpeg"]
        assert env.read_keys() == [good_0["ref"], good_1["ref"],
                                   bad_0["ref"], bad_1["ref"]]
        assert [image["bytes"] for image in images] == [
            env.example_bytes[ref["ref"]]
            for ref in (good_0, good_1, bad_0, bad_1)]

    def test_omitted_refs_are_never_read(self, env):
        """Req 7.4: with a limit of 3, two examples attach and the
        remaining three are never fetched."""
        env.set_image_limit(3)
        good = [env.put_example(GOOD, index) for index in range(3)]
        bad = [env.put_example(BAD, index) for index in range(2)]

        images, limit = env.resolve(
            few_shot={"enabled": True, "examples": good + bad})

        assert limit == 3
        assert len(images) == 2
        attached_refs = [good[0]["ref"], good[1]["ref"]]
        assert env.read_keys() == attached_refs
        omitted_refs = {good[2]["ref"], bad[0]["ref"], bad[1]["ref"]}
        assert omitted_refs.isdisjoint(set(env.read_keys()))

    def test_limit_of_one_attaches_and_reads_nothing(self, env):
        """Req 7.4: the target image consumes the only slot."""
        env.set_image_limit(1)
        examples = [env.put_example(GOOD, 0), env.put_example(BAD, 0)]

        images, limit = env.resolve(
            few_shot={"enabled": True, "examples": examples})

        assert limit == 1
        assert images == []
        assert env.get_object_calls == []
        assert env.client_requests == []

    @pytest.mark.parametrize("raw", [
        pytest.param(None, id="unset"),
        pytest.param("", id="blank"),
        pytest.param("   ", id="whitespace"),
        pytest.param("not json", id="invalid-json"),
        pytest.param("[]", id="json-array"),
        pytest.param("null", id="json-null"),
        pytest.param('{"other-model": 3}', id="other-model"),
        pytest.param(f'{{"{MODEL_ID}": 0}}', id="zero"),
        pytest.param(f'{{"{MODEL_ID}": "5"}}', id="string-value"),
    ])
    def test_default_limit_for_absent_or_unusable_configuration(self, env,
                                                                raw):
        """Req 7.1: configuration can never widen or zero the bound."""
        if raw is None:
            env.monkeypatch.delenv("LLM_MODEL_IMAGE_LIMITS", raising=False)
        else:
            env.monkeypatch.setenv("LLM_MODEL_IMAGE_LIMITS", raw)

        _images, limit = env.resolve(few_shot={"enabled": False})

        assert limit == DEFAULT_LIMIT

    def test_malformed_entries_never_consume_a_slot(self, env):
        """Malformed references are dropped *before* selection, so the
        attached prefix is made of well-formed refs only."""
        env.set_image_limit(3)
        good_0 = env.put_example(GOOD, 0)
        good_1 = env.put_example(GOOD, 1)
        stored = ["not-a-dict", {"ref": ""}, good_0,
                  {"designation": GOOD}, good_1]

        images, _limit = env.resolve(
            few_shot={"enabled": True, "examples": stored})

        assert len(images) == 2
        assert env.read_keys() == [good_0["ref"], good_1["ref"]]

    def test_one_client_per_bucket(self, env):
        """Attached refs share one S3 client per bucket."""
        artifacts = [env.put_example(GOOD, index) for index in range(2)]
        dataset = env.put_example(BAD, 0, bucket=DATASET_BUCKET)

        images, _limit = env.resolve(
            few_shot={"enabled": True, "examples": artifacts + [dataset]})

        assert len(images) == 3
        assert env.client_requests == [ARTIFACTS_BUCKET, DATASET_BUCKET]


# ---------------------------------------------------- unreadable examples

class TestUnreadableExampleReason:
    """Req 6.7: the reason names the reference."""

    def test_missing_object_reason_names_the_ref(self, env):
        good = env.put_example(GOOD, 0)
        missing = env.missing_example(BAD, 0)

        with pytest.raises(env.worker.GenerationFailure) as excinfo:
            env.resolve(few_shot={"enabled": True,
                                  "examples": [good, missing]})

        reason = str(excinfo.value)
        prefix = (f"few-shot example image {missing['ref']} is not "
                  f"accessible: ")
        assert reason.startswith(prefix)
        assert len(reason) > len(prefix)          # the cause is appended

    def test_unresolvable_reference_reason_is_exact(self, env):
        """A non-empty ref that resolves to no S3 object gets the
        dedicated reason, still naming the ref."""
        with pytest.raises(env.worker.GenerationFailure) as excinfo:
            env.resolve(few_shot={
                "enabled": True,
                "examples": [{"ref": "s3://", "designation": GOOD,
                              "position": 0}]})

        assert str(excinfo.value) == (
            "few-shot example image s3:// is not accessible: the reference "
            "could not be resolved to an S3 object")

    def test_omitted_unreadable_ref_never_fails_the_image(self, env):
        """An unreadable reference beyond the bound is never read, so it
        cannot fail the image."""
        env.set_image_limit(2)
        good = env.put_example(GOOD, 0)
        missing = env.missing_example(BAD, 0)

        images, limit = env.resolve(
            few_shot={"enabled": True, "examples": [good, missing]})

        assert limit == 2
        assert [image["designation"] for image in images] == [GOOD]
        assert env.read_keys() == [good["ref"]]


# --------------------------------------------- end-to-end through the SQS
#                                               handler (batch semantics)

class TestBatchEndToEnd:
    """Req 6.5, 6.7, 10.3 through the real handler: an unreadable
    example fails only its own dataset image."""

    def test_unreadable_example_fails_only_its_own_dataset_image(self, env):
        missing = env.missing_example(GOOD, 0)
        broken_job = env.make_llm_job(
            few_shot={"enabled": True, "examples": [missing]})
        broken_uri = env.put_image(f"imgs/{uuid.uuid4()}.png",
                                   width=WIDTH, height=HEIGHT)
        broken_task = env.make_task(broken_job, broken_uri)

        good = env.put_example(GOOD, 0)
        bad = env.put_example(BAD, 0, ext="jpg")
        enabled_job = env.make_llm_job(
            few_shot={"enabled": True, "examples": [bad, good]})
        enabled_uri = env.put_image(f"imgs/{uuid.uuid4()}.png",
                                    width=WIDTH, height=HEIGHT)
        enabled_task = env.make_task(enabled_job, enabled_uri)

        # No `few_shot` key at all: the pre-feature request (Req 10.3).
        absent_job = env.make_llm_job()
        absent_uri = env.put_image(f"imgs/{uuid.uuid4()}.png",
                                   width=WIDTH, height=HEIGHT)
        absent_task = env.make_task(absent_job, absent_uri)

        bedrock, _ = env.use_bedrock(replies=[guidance([BOX])])

        result = env.run([
            env.record(broken_job, broken_task, broken_uri,
                       "ObjectDetection", LABELS, MODEL,
                       detection_prompt=PROMPT),
            env.record(enabled_job, enabled_task, enabled_uri,
                       "ObjectDetection", LABELS, MODEL,
                       detection_prompt=PROMPT),
            env.record(absent_job, absent_task, absent_uri,
                       "ObjectDetection", LABELS, MODEL,
                       detection_prompt=PROMPT),
        ])

        # The unreadable example fails only its own image; the batch
        # loop and partial-batch semantics are unchanged.
        assert result == {"batchItemFailures": []}
        failed = env.get_task(broken_job, broken_task)
        assert failed["prelabel_status"] == "Failed"
        assert failed["prelabel_error"].startswith(
            f"few-shot example image {missing['ref']} is not accessible: ")
        assert not env.prelabel_exists(broken_job, broken_task)

        assert (env.get_task(enabled_job, enabled_task)["prelabel_status"]
                == "Available")
        assert (env.get_task(absent_job, absent_task)["prelabel_status"]
                == "Available")

        # The broken job issued no model invocation; the two healthy
        # records issued one each, in record order.
        assert len(bedrock.calls) == 2
        few_shot_content = bedrock.calls[0]["messages"][0]["content"]
        absent_content = bedrock.calls[1]["messages"][0]["content"]

        # Good example first, then the bad one, each identified in a text
        # block immediately preceding its image (Req 6.5).
        assert [few_shot_content[index]["text"] for index in (0, 1, 3)] == [
            FEW_SHOT_HEADER, "Good example 1:", "Bad example 1:"]
        assert few_shot_content[2]["image"]["source"]["bytes"] == (
            env.example_bytes[good["ref"]])
        assert few_shot_content[2]["image"]["format"] == "png"
        assert few_shot_content[4]["image"]["source"]["bytes"] == (
            env.example_bytes[bad["ref"]])
        assert few_shot_content[4]["image"]["format"] == "jpeg"
        # Target image and prompt keep the pre-feature suffix.
        assert few_shot_content[5]["text"] == FEW_SHOT_TARGET_INTRO
        assert few_shot_content[6]["image"]["source"]["bytes"].startswith(
            b"\x89PNG")
        assert PROMPT in few_shot_content[7]["text"]
        assert len(few_shot_content) == 8

        # Req 10.3: a job record without a few_shot document builds the
        # pre-feature two-block request.
        assert len(absent_content) == 2
        assert absent_content[0]["image"]["source"]["bytes"].startswith(
            b"\x89PNG")
        assert PROMPT in absent_content[1]["text"]
        assert "example" not in absent_content[1]["text"].lower()


# --------------------------------------------- job-record sizing reads
#                     (llm-model-token-and-image-sizing, task 8.2 —
#                     Req 3.8, 5.8, 5.9, 5.12, 8.1, 8.5, 9.2, 10.10)

# The shared-layer Model_Token_Limit_Default: every request whose
# Token_Budget_Selection and Model_Token_Limits entry are both invalid
# or absent must carry exactly this maxTokens (Req 3.8).
DEFAULT_TOKEN_BUDGET = 10000


def real_png(width, height):
    """A fully decodable PNG — unlike `png_bytes`' header-only bytes —
    for the cases that drive the Image_Downscaler through a real
    decode-and-re-encode (test_dda_llm_prelabel.real_png_bytes's
    convention)."""
    from PIL import Image  # lazy, matching the imaging-layer convention

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 40, 200)).save(buffer,
                                                           format="PNG")
    return buffer.getvalue()


def decoded_size(image_bytes):
    """(width, height) of a Converse image block's bytes, via a real
    decode."""
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as image:
        return image.size


def put_real_example(env, designation, position, *, width, height):
    """One fully decodable PNG example upload — `put_example`'s stored
    shape with real pixel data, for jobs whose Downscale_Setting makes
    the downscaler decode the example bytes."""
    key = (f"labeling-examples/{uuid.uuid4().hex[:8]}/{designation}/"
           f"{position}.png")
    body = real_png(width, height)
    env.s3.put_object(Bucket=ARTIFACTS_BUCKET, Key=key, Body=body)
    env.example_bytes[key] = body
    return {"ref": key, "designation": designation, "position": position}


def make_sized_llm_job(env, downscale_max_edge=_ABSENT, token_budget=_ABSENT,
                       few_shot=_ABSENT):
    """A persisted `llm:` job carrying the two sizing values, written
    through DynamoDB so the worker reads them back exactly as production
    does — numbers as Decimal (Req 5.8, 3.7)."""
    job_id = env.make_llm_job(few_shot=few_shot)
    for name, value in (("downscale_max_edge", downscale_max_edge),
                        ("token_budget", token_budget)):
        if value is not _ABSENT:
            env.stack.tables.labeling_jobs.update_item(
                Key={"job_id": job_id},
                UpdateExpression=f"SET auto_label.{name} = :value",
                ExpressionAttributeValues={":value": value})
    return job_id


def run_llm_task(env, job_id, image_body=None):
    """One image + task + record for `job_id` through the real handler,
    returning the recorded state for assertions."""
    uri = env.put_image(f"imgs/{uuid.uuid4()}.png", width=WIDTH,
                        height=HEIGHT, body=image_body)
    task_id = env.make_task(job_id, uri)
    bedrock, _ = env.use_bedrock(replies=[guidance([BOX])])
    result = env.run([env.record(job_id, task_id, uri, "ObjectDetection",
                                 LABELS, MODEL, detection_prompt=PROMPT)])
    return SimpleNamespace(result=result, task_id=task_id, bedrock=bedrock)


@pytest.fixture
def sizing_env(env):
    """FewShotEnv pinned to the environment-bootstrap Model_Token_Limits
    source: no settings-table read (other test modules leave
    SETTINGS_TABLE set in os.environ) and no inherited mapping, so every
    budget assertion resolves against exactly what its test configures."""
    env.monkeypatch.setattr(env.worker, "SETTINGS_TABLE", None)
    env.monkeypatch.delenv("LLM_MODEL_TOKEN_LIMITS", raising=False)
    return env


class TestJobRecordSizingReads:
    """Req 5.8, 3.8: `auto_label.downscale_max_edge` and
    `auto_label.token_budget` are read off the job record — through the
    Decimal conversion DynamoDB imposes — and reach the request."""

    def test_persisted_setting_and_budget_reach_the_request(self,
                                                            sizing_env):
        """A valid persisted setting downscales the target and a valid
        persisted budget is the request's maxTokens, unchanged by what
        the Model_Token_Limits mapping says today (Req 5.8, 3.7). Both
        arrive as Decimal, so this also pins the conversion — without it
        each value would silently degrade to Off / the mapping."""
        env = sizing_env
        env.monkeypatch.setenv("LLM_MODEL_TOKEN_LIMITS",
                               json.dumps({MODEL_ID: 64000}))
        job_id = make_sized_llm_job(env, downscale_max_edge=512,
                                    token_budget=20000)

        run = run_llm_task(env, job_id, image_body=real_png(1000, 600))

        assert run.result == {"batchItemFailures": []}
        assert (env.get_task(job_id, run.task_id)["prelabel_status"]
                == "Available")
        assert len(run.bedrock.calls) == 1
        call = run.bedrock.calls[0]
        # The persisted budget wins the resolution over the mapping.
        assert call["inferenceConfig"]["maxTokens"] == 20000
        content = call["messages"][0]["content"]
        # The target is sent at the record's bound: 1000x600 at 512
        # floors to 512x307, and the prompt names those Sent_Dimensions.
        assert decoded_size(content[0]["image"]["source"]["bytes"]) == (
            512, 307)
        assert "512 pixels wide" in content[1]["text"]
        assert "307 pixels tall" in content[1]["text"]
        # The stored Pre_Label stays in Source coordinate space.
        prelabel = env.prelabel_json(job_id, run.task_id)
        assert prelabel["image_width"] == 1000
        assert prelabel["image_height"] == 600

    @pytest.mark.parametrize("stored", [
        pytest.param(_ABSENT, id="absent"),
        pytest.param(None, id="null"),
        pytest.param(True, id="boolean"),
        pytest.param("1024", id="string"),
        pytest.param(999, id="not-an-option"),
        pytest.param(Decimal("512.5"), id="fractional-number"),
    ])
    def test_malformed_or_absent_downscale_is_downscale_off(self, sizing_env,
                                                            stored):
        """Req 5.9, 5.12, 10.10: a malformed, null or absent
        `downscale_max_edge` is Downscale_Off with no failure. The
        header-only 3000x2000 target is undecodable, so any
        wrongly-applied bound would refuse it — pass-through is the only
        way this task reaches Available."""
        env = sizing_env
        job_id = make_sized_llm_job(env, downscale_max_edge=stored)
        body = png_bytes(3000, 2000)

        run = run_llm_task(env, job_id, image_body=body)

        assert run.result == {"batchItemFailures": []}
        assert (env.get_task(job_id, run.task_id)["prelabel_status"]
                == "Available")
        content = run.bedrock.calls[0]["messages"][0]["content"]
        # Byte-identical pass-through; Sent equals Source at Off.
        assert content[0]["image"]["source"]["bytes"] == body
        assert "3000 pixels wide" in content[1]["text"]
        assert "2000 pixels tall" in content[1]["text"]

    @pytest.mark.parametrize("stored", [
        pytest.param(_ABSENT, id="absent"),
        pytest.param(None, id="null"),
        pytest.param(True, id="boolean"),
        pytest.param("20000", id="string"),
        pytest.param(0, id="zero"),
        pytest.param(128001, id="above-ceiling"),
        pytest.param(Decimal("9999.5"), id="fractional-number"),
    ])
    def test_malformed_or_absent_budget_resolves_the_default(self, sizing_env,
                                                             stored):
        """Req 3.8, 10.10: with no Model_Token_Limits entry for the
        model, a malformed or absent `token_budget` resolves the default
        of 10000 — never a failure, never the Global_Max_Tokens."""
        env = sizing_env
        job_id = make_sized_llm_job(env, token_budget=stored)

        run = run_llm_task(env, job_id)

        assert run.result == {"batchItemFailures": []}
        assert (env.get_task(job_id, run.task_id)["prelabel_status"]
                == "Available")
        assert len(run.bedrock.calls) == 1
        assert (run.bedrock.calls[0]["inferenceConfig"]["maxTokens"]
                == DEFAULT_TOKEN_BUDGET)

    def test_malformed_budget_falls_through_to_the_mapping(self, sizing_env):
        """Req 3.8: the invalid persisted selection falls through to the
        Model_Token_Limits entry, not straight to the default."""
        env = sizing_env
        env.monkeypatch.setenv("LLM_MODEL_TOKEN_LIMITS",
                               json.dumps({MODEL_ID: 64000}))
        job_id = make_sized_llm_job(env, token_budget=True)

        run = run_llm_task(env, job_id)

        assert run.result == {"batchItemFailures": []}
        assert (env.get_task(job_id, run.task_id)["prelabel_status"]
                == "Available")
        assert run.bedrock.calls[0]["inferenceConfig"]["maxTokens"] == 64000


class TestExampleDownscaling:
    """Req 8.1: every attached Few_Shot_Example is downscaled with the
    target's setting, in the layout the setting leaves untouched."""

    def test_examples_downscaled_with_the_targets_setting(self, sizing_env):
        env = sizing_env
        oversize = put_real_example(env, GOOD, 0, width=1200, height=800)
        fitting = put_real_example(env, BAD, 0, width=300, height=200)
        job_id = make_sized_llm_job(
            env, downscale_max_edge=512,
            few_shot={"enabled": True, "examples": [oversize, fitting]})

        run = run_llm_task(env, job_id, image_body=real_png(1000, 600))

        assert run.result == {"batchItemFailures": []}
        assert (env.get_task(job_id, run.task_id)["prelabel_status"]
                == "Available")
        content = run.bedrock.calls[0]["messages"][0]["content"]
        # The few-shot layout is unchanged by the setting.
        assert [content[index]["text"] for index in (0, 1, 3, 5)] == [
            FEW_SHOT_HEADER, "Good example 1:", "Bad example 1:",
            FEW_SHOT_TARGET_INTRO]
        assert len(content) == 8
        # The oversize example carries re-encoded bytes at the target's
        # bound: 1200x800 at 512 floors to 512x341.
        good_bytes = content[2]["image"]["source"]["bytes"]
        assert good_bytes != env.example_bytes[oversize["ref"]]
        assert decoded_size(good_bytes) == (512, 341)
        assert content[2]["image"]["format"] == "png"
        # The already-fitting example passes through byte-identically.
        assert content[4]["image"]["source"]["bytes"] == (
            env.example_bytes[fitting["ref"]])
        # The target is downscaled with the same setting, and the prompt
        # names its Sent_Dimensions alone — no example dimensions.
        assert decoded_size(content[6]["image"]["source"]["bytes"]) == (
            512, 307)
        assert "512 pixels wide" in content[7]["text"]
        assert "307 pixels tall" in content[7]["text"]


class TestRefusedExampleBatchContinuation:
    """Req 8.5, 9.2 through the real SQS handler: an example the
    Image_Downscaler refuses fails only its own dataset image, with no
    model invocation for it, while the batch continues."""

    def test_refused_example_fails_only_its_own_dataset_image(self,
                                                              sizing_env):
        env = sizing_env
        # Header-only bytes declaring 2000x1500 — above the 512 bound,
        # so the downscaler must decode them, and cannot.
        undecodable = env.put_example(GOOD, 0, width=2000, height=1500)
        broken_job = make_sized_llm_job(
            env, downscale_max_edge=512,
            few_shot={"enabled": True, "examples": [undecodable]})
        broken_uri = env.put_image(f"imgs/{uuid.uuid4()}.png",
                                   width=WIDTH, height=HEIGHT)
        broken_task = env.make_task(broken_job, broken_uri)

        # Same setting with a decodable example: the sibling succeeds.
        healthy = put_real_example(env, GOOD, 0, width=1200, height=800)
        healthy_job = make_sized_llm_job(
            env, downscale_max_edge=512,
            few_shot={"enabled": True, "examples": [healthy]})
        healthy_uri = env.put_image(f"imgs/{uuid.uuid4()}.png",
                                    width=WIDTH, height=HEIGHT)
        healthy_task = env.make_task(healthy_job, healthy_uri)

        # Neither sizing value on the record: the pre-feature request.
        plain_job = make_sized_llm_job(env)
        plain_uri = env.put_image(f"imgs/{uuid.uuid4()}.png",
                                  width=WIDTH, height=HEIGHT)
        plain_task = env.make_task(plain_job, plain_uri)

        bedrock, _ = env.use_bedrock(replies=[guidance([BOX])])

        result = env.run([
            env.record(broken_job, broken_task, broken_uri,
                       "ObjectDetection", LABELS, MODEL,
                       detection_prompt=PROMPT),
            env.record(healthy_job, healthy_task, healthy_uri,
                       "ObjectDetection", LABELS, MODEL,
                       detection_prompt=PROMPT),
            env.record(plain_job, plain_task, plain_uri,
                       "ObjectDetection", LABELS, MODEL,
                       detection_prompt=PROMPT),
        ])

        # The refusal is absorbed per record: no batch item failure, and
        # only the broken job's task is Failed (Req 9.2).
        assert result == {"batchItemFailures": []}
        failed = env.get_task(broken_job, broken_task)
        assert failed["prelabel_status"] == "Failed"
        # The reason names the example and the requested bound (Req 8.5).
        assert failed["prelabel_error"].startswith(
            "few-shot example image at position 1 could not be resized "
            "to a longer edge of 512 pixels: ")
        assert not env.prelabel_exists(broken_job, broken_task)

        assert (env.get_task(healthy_job, healthy_task)["prelabel_status"]
                == "Available")
        assert (env.get_task(plain_job, plain_task)["prelabel_status"]
                == "Available")

        # The broken record invoked no model (Req 8.5); the two healthy
        # records issued one call each, in record order, each with its
        # own sizing behavior intact.
        assert len(bedrock.calls) == 2
        healthy_content = bedrock.calls[0]["messages"][0]["content"]
        assert decoded_size(
            healthy_content[2]["image"]["source"]["bytes"]) == (512, 341)
        plain_content = bedrock.calls[1]["messages"][0]["content"]
        assert len(plain_content) == 2
