"""
Grounded-SAM Preview_Executor property tests.

Spec: grounded-sam-prompt-tuning-preview, task 1.3.

Five properties over the real `dda_labeling.execute_preview_run`
grounded-sam sample path, against the moto-backed stack from conftest.py
(real shared_utils cross-account fallback, real DynamoDB tables, the
real portal artifacts bucket), 100 Hypothesis examples per property.
Runs are seeded directly in the shipped item shapes through the module's
own `_write_preview_run_item` / `_write_preview_sample_items` writers —
no route in the loop — and the worker is a fake Lambda client injected
at the module's `grounded_sam_lambda_client` test injection point (the
test_property_grounded_sam_consumer.py FakeGroundedSamLambdaClient
pattern, extended with per-invocation scripting and a raising arm).

**Feature: grounded-sam-prompt-tuning-preview, Property 5: The
executor's invoke payload equals the consumer's derivation** — *For
any* Label_Set and recorded `prompt_overrides` (mixing surviving,
blank, absent, and malformed entries), each Sample_Image invocation of
a Grounded_SAM_Preview_Run SHALL carry a payload whose `prompts` equal
`dda_autolabel_worker._grounded_sam_prompts(label_set, overrides)`,
whose `modality` is the run's task type, and whose
`image_s3_presigned_url` presigns the sample's object — so each run's
prompts are its own recorded entries.
**Validates: Requirements 2.2, 4.1, 7.1**

**Feature: grounded-sam-prompt-tuning-preview, Property 6: Response
acceptance and payload construction mirror the consumer's validation**
— *For any* worker response payload (valid Segmentation/ObjectDetection
responses including empty `regions`, and malformed ones:
missing/non-list `regions`, non-integer dimensions, out-of-Label_Set
classes, empty RLE, missing/degenerate/out-of-bounds box geometry), the
preview sample SHALL resolve Succeeded exactly when
`_generate_grounded_sam_prelabel` would accept the same payload, and on
success the written prelabel SHALL carry the renderer shapes with each
region/box's `score` present exactly when the worker returned one, an
empty response resolving as Succeeded with an empty Pre_Label.
**Validates: Requirements 4.2, 4.3, 4.4**

**Feature: grounded-sam-prompt-tuning-preview, Property 7: Failure
categorization is total, one existing category per outcome, worker
detail carried** — *For any* per-sample invocation outcome (invocation
exception, read timeout, `FunctionError`, unparseable payload,
validation-failing payload, presign failure), the sample SHALL resolve
Failed with exactly one category from the existing
`PREVIEW_FAILURE_CATEGORIES` — `timeout` for timeouts,
`image_access_failure` for presign failures, `model_error` otherwise —
with a reason carrying the outcome's detail, and every other sample of
the run SHALL resolve independently.
**Validates: Requirements 4.5, 4.6**

**Feature: grounded-sam-prompt-tuning-preview, Property 8: Every
grounded-sam run terminates: deadline-guarded samples resolve as
timeout without invocation, the run reaches Completed, the lock is
released** — *For any* sample count 1..5 and any remaining-time profile
of the executor's Lambda context (including profiles that exhaust
mid-run), every sample SHALL reach a resolution — samples whose slot
cannot fit another 240-second invocation resolving as `timeout`
failures with zero worker invocations — the run status SHALL reach
Completed, and the in-flight lock SHALL be absent afterwards.
**Validates: Requirements 4.7, 4.8**

**Feature: grounded-sam-prompt-tuning-preview, Property 10: Grounded-sam
runs create no labeling-pipeline state** — *For any* executed
Grounded_SAM_Preview_Run (mixed success/failure outcomes), the system
SHALL hold no new Labeling_Job record, no Task_Assignment item, no
artifact under `labeling/{usecase_id}/`, and no labeler notification —
result payloads existing only under `labeling-previews/`.
**Validates: Requirements 4.9, 9.3**

Oracles
-------
Properties 5 and 6 import the *consumer's* functions as oracles — that
is the point: `dda_autolabel_worker._grounded_sam_prompts` is called
directly (it is pure), and `dda_autolabel_worker.
_generate_grounded_sam_prelabel` is driven with its own injected fake
client over the identical worker payload, so any drift between the
preview's replicated derivation/validation and labeling time is
test-detected. The consumer module is imported the way the consumer
suites import it: inside the moto mock with
GROUNDED_SAM_WORKER_FUNCTION_NAME set.

Harness reuse (Hypothesis cannot consume function-scoped fixtures): the
module-scoped `genv` fixture imports both real modules inside the moto
mock; per-example runs are seeded with uuid-fresh run ids and user
subs, so examples never interfere.
"""
import io
import json
import os
import string
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

REGION = "us-east-1"
ARTIFACTS_BUCKET = "test-portal-artifacts"
DATASET_BUCKET = "test-gsam-preview-data"
GS_FUNCTION = "test-dda-grounded-sam-worker"

GEOMETRY_MODALITIES = ("Segmentation", "ObjectDetection")

# == dda_labeling.PREVIEW_GSAM_DEADLINE_GUARD_MILLIS ((240 + 30) * 1000);
# restated rather than imported so a drifted threshold fails the test.
DEADLINE_GUARD_MILLIS = 270_000
DEADLINE_REASON = ("the preview run deadline was reached before this "
                   "sample could be invoked")

# A worker response every arm accepts — the succeeding-sibling filler.
VALID_EMPTY = {"regions": [], "image_width": 64, "image_height": 48}

# Sentinel distinguishing "no prompt_overrides attribute on the RUN
# item" (what an override-free start writes) from every explicit value.
_ABSENT = object()


# ------------------------------------------------------------- fake client

class FakeGroundedSamLambdaClient:
    """Records synchronous Grounded-SAM worker invocations and replays
    one scripted outcome per invocation, in order (the last repeats) —
    the test_property_grounded_sam_consumer.py pattern extended with
    per-invocation scripting and a raising arm.

    Outcome arms: `{'payload': ...}` JSON-encodes a response,
    `{'raw_payload': bytes}` returns the bytes verbatim,
    `{'function_error': msg}` answers a Lambda FunctionError whose body
    is `{"errorMessage": msg}`, `{'raise': exc}` raises `exc` from the
    invoke call itself.
    """

    def __init__(self, outcomes):
        self.invocations = []
        self.outcomes = list(outcomes)

    def invoke(self, **kwargs):
        if not self.outcomes:
            raise AssertionError(
                "Grounded-SAM worker invoked with no scripted outcome")
        index = min(len(self.invocations), len(self.outcomes) - 1)
        self.invocations.append(kwargs)
        outcome = self.outcomes[index]
        if "raise" in outcome:
            raise outcome["raise"]
        if "function_error" in outcome:
            return {
                "StatusCode": 200,
                "FunctionError": "Unhandled",
                "Payload": io.BytesIO(json.dumps(
                    {"errorMessage": outcome["function_error"]}).encode()),
            }
        if "raw_payload" in outcome:
            body = outcome["raw_payload"]
        else:
            body = json.dumps(outcome["payload"]).encode()
        return {"StatusCode": 200, "Payload": io.BytesIO(body)}


class ScriptedContext:
    """Stub Lambda context whose `get_remaining_time_in_millis` replays
    a per-call profile (the last value repeats), recording how often the
    Run_Deadline_Guard consulted it."""

    def __init__(self, remaining_millis):
        self.function_name = "test-dda-labeling-gsam-preview"
        self._remaining = list(remaining_millis)
        self.calls = 0

    def get_remaining_time_in_millis(self):
        value = self._remaining[min(self.calls, len(self._remaining) - 1)]
        self.calls += 1
        return value


# ------------------------------------------------------------------ harness

class ExecutorEnv:
    """One shared Use_Case; per-example grounded-sam runs seeded in the
    shipped item shapes through the module's own writers; fake-client
    injection at `dda_labeling.grounded_sam_lambda_client`; the consumer
    module alongside as the oracle."""

    def __init__(self, stack, module, worker):
        self.stack = stack
        self.module = module
        self.worker = worker
        self.tasks = stack.tables.labeling_tasks
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        # Single-account use case: the root cross_account_role_arn makes
        # get_s3_client_for_bucket fall back to default (moto) creds on
        # both the preview and the consumer presign paths.
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Grounded-SAM Preview Executor Property Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })
        # The oracle side presigns (never reads) one shared image.
        oracle_key = f"oracle/{uuid.uuid4()}.png"
        self.s3.put_object(Bucket=DATASET_BUCKET, Key=oracle_key,
                           Body=b"\x89PNG-bytes-never-read")
        self.oracle_image_uri = f"s3://{DATASET_BUCKET}/{oracle_key}"
        self.oracle_job = {"usecase_id": self.usecase_id,
                           "auto_label": {"enabled": True,
                                          "model": "grounded-sam"}}

    # ------------------------------------------------------------ seeding
    def seed_run(self, task_type, label_set, sample_count,
                 overrides=_ABSENT):
        """A Running grounded-sam RUN item plus one Pending IMAGE#{i}
        item per sample, written by the shipped writers (the exact item
        shapes the start route produces for the family: no
        detection_prompt, few_shot_enabled False, zero example counts).

        `overrides` other than `_ABSENT` is written onto the RUN item
        afterwards, so malformed recorded states (blank values, junk
        types, non-dict attributes — the hand-edited-record class the
        consumer's own property suite covers) are expressible beside the
        surviving maps the start route records.
        """
        run_id = self.module._new_preview_run_id()
        user_sub = f"user-{uuid.uuid4().hex[:8]}"
        sample_keys = [f"previews/{run_id}/s{index}.png"
                       for index in range(sample_count)]
        self.module._write_preview_run_item(
            run_id, self.usecase_id, user_sub, "grounded-sam", task_type,
            list(label_set), None, sample_count, False, 0)
        if overrides is not _ABSENT:
            self.tasks.update_item(
                Key={"job_id": f"PREVIEW#{run_id}", "task_id": "RUN"},
                UpdateExpression="SET prompt_overrides = :overrides",
                ExpressionAttributeValues={":overrides": overrides})
        self.module._write_preview_sample_items(run_id, sample_keys)
        return SimpleNamespace(run_id=run_id, user_sub=user_sub,
                               sample_keys=sample_keys)

    def claim_lock(self, run):
        """The in-flight claim the start route would have made."""
        assert self.module._claim_preview_lock(
            self.usecase_id, run.user_sub, run.run_id,
            len(run.sample_keys), "grounded-sam")

    # -------------------------------------------------------------- seams
    def use_preview_client(self, outcomes):
        """Inject a fake worker client at the module's seam."""
        fake = FakeGroundedSamLambdaClient(outcomes)
        self.module.grounded_sam_lambda_client = fake
        return fake

    def oracle(self, modality, label_set, payload):
        """`(accepted, prelabel)` from the consumer's real
        `_generate_grounded_sam_prelabel` over the identical worker
        payload, driven through the consumer's own injection seam."""
        self.worker.grounded_sam_lambda_client = FakeGroundedSamLambdaClient(
            [{"payload": payload}])
        try:
            prelabel = self.worker._generate_grounded_sam_prelabel(
                {"modality": modality, "label_set": list(label_set),
                 "image_s3_uri": self.oracle_image_uri},
                self.oracle_job)
            return True, prelabel
        except self.worker.GenerationFailure:
            return False, None
        finally:
            self.worker.grounded_sam_lambda_client = None

    # ---------------------------------------------------------- execution
    def execute(self, run, context=None):
        return self.module.execute_preview_run(run.run_id, context=context)

    # ----------------------------------------------------------- readback
    def run_item(self, run):
        return self.tasks.get_item(
            Key={"job_id": f"PREVIEW#{run.run_id}",
                 "task_id": "RUN"}).get("Item")

    def sample_items(self, run):
        return self.module._read_preview_sample_items(run.run_id)

    def lock_item(self, run):
        return self.tasks.get_item(Key={
            "job_id": f"PREVIEWLOCK#{self.usecase_id}",
            "task_id": f"USER#{run.user_sub}"}).get("Item")

    def payload(self, run, index):
        key = (f"labeling-previews/{self.usecase_id}/"
               f"{run.run_id}/{index}.json")
        body = self.s3.get_object(Bucket=ARTIFACTS_BUCKET,
                                  Key=key)["Body"].read()
        return json.loads(body.decode("utf-8"))

    def artifact_keys(self, prefix=""):
        keys, token = set(), None
        while True:
            kwargs = {"Bucket": ARTIFACTS_BUCKET, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = self.s3.list_objects_v2(**kwargs)
            keys.update(item["Key"]
                        for item in response.get("Contents", []))
            if not response.get("IsTruncated"):
                return keys
            token = response.get("NextContinuationToken")

    def task_keys(self):
        keys, kwargs = set(), {}
        while True:
            response = self.tasks.scan(**kwargs)
            for item in response.get("Items", []):
                keys.add((item["job_id"], item["task_id"]))
            if not response.get("LastEvaluatedKey"):
                return keys
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def job_ids(self):
        ids, kwargs = set(), {}
        while True:
            response = self.stack.tables.labeling_jobs.scan(**kwargs)
            for item in response.get("Items", []):
                ids.add(item["job_id"])
            if not response.get("LastEvaluatedKey"):
                return ids
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


@pytest.fixture(scope="module")
def genv(aws_stack):
    """The real dda_labeling and dda_autolabel_worker modules imported
    inside the moto mock with the Grounded-SAM worker function name
    configured (the test_property_grounded_sam_consumer.py `genv`
    convention, incl. displacing a collection-time fake shared_utils),
    wrapped in an ExecutorEnv."""
    shared = sys.modules.get("shared_utils")
    if shared is not None and not hasattr(shared,
                                          "get_s3_client_for_bucket"):
        sys.modules.pop("shared_utils")

    saved_function = os.environ.get("GROUNDED_SAM_WORKER_FUNCTION_NAME")
    os.environ["GROUNDED_SAM_WORKER_FUNCTION_NAME"] = GS_FUNCTION

    sys.modules.pop("dda_labeling", None)
    import dda_labeling

    # Both modules read the env at import; make sure the test values
    # stuck whatever the ambient environment was.
    dda_labeling.GROUNDED_SAM_WORKER_FUNCTION_NAME = GS_FUNCTION
    dda_labeling.PORTAL_ARTIFACTS_BUCKET = ARTIFACTS_BUCKET

    sys.modules.pop("dda_autolabel_worker", None)
    import dda_autolabel_worker as worker
    worker.GROUNDED_SAM_WORKER_FUNCTION_NAME = GS_FUNCTION

    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=DATASET_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass

    env = ExecutorEnv(aws_stack, dda_labeling, worker)
    yield env
    dda_labeling.grounded_sam_lambda_client = None
    worker.grounded_sam_lambda_client = None
    if saved_function is None:
        os.environ.pop("GROUNDED_SAM_WORKER_FUNCTION_NAME", None)
    else:
        os.environ["GROUNDED_SAM_WORKER_FUNCTION_NAME"] = saved_function


# -------------------------------------------------------------- generators

# Printable unicode without surrogates — the sibling property suites'
# alphabet (Latin, Greek, Cyrillic, CJK punctuation, symbols).
_TEXT_ALPHABET = st.characters(min_codepoint=32, max_codepoint=0x2FFF,
                               blacklist_categories=("Cs",))

# Valid Label_Set names: non-empty, stable under strip (job creation
# persists stripped names; the RUN item carries them pre-stripped).
_label_names = st.text(alphabet=_TEXT_ALPHABET, min_size=1,
                       max_size=24).map(str.strip).filter(bool)

_label_sets = st.lists(_label_names, min_size=1, max_size=5, unique=True)

_rle_strings = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=24)

_scores = st.one_of(
    st.floats(min_value=0, max_value=1, allow_nan=False,
              allow_infinity=False),
    st.integers(min_value=0, max_value=1),
)

# Override values a RUN item can physically carry (DynamoDB has no raw
# float type and map keys are strings): strings incl. blank-after-trim
# and unicode, plus the non-string shapes a hand-edited record could
# hold (null, bool, number, list) — all of which must degrade to the
# label-name fallback on both the preview and the consumer derivations.
_record_override_values = st.one_of(
    st.text(alphabet=_TEXT_ALPHABET, max_size=48),
    st.sampled_from(["", " ", "  \u00a0 "]),
    st.none(),
    st.booleans(),
    st.integers(min_value=-10**9, max_value=10**9),
    st.lists(st.text(alphabet=_TEXT_ALPHABET, max_size=6), max_size=3),
)

# Entire non-dict prompt_overrides attribute values — the derivation is
# total over them on both sides.
_non_dict_record_overrides = st.sampled_from([None, "not-a-map", 7, True])

# ASCII-only failure details, so the detail survives the JSON escaping
# of a FunctionError body verbatim and "detail in reason" is exact.
_ascii_details = st.text(
    alphabet=st.sampled_from(string.ascii_letters + string.digits + " _-"),
    min_size=1, max_size=48).filter(lambda text: text.strip())


def _override_states(labels):
    """Absent (no attribute), a string-keyed map mixing in-Label_Set and
    extra keys with conforming and junk values, or a non-dict value."""
    keys = st.one_of(
        st.sampled_from(labels),
        st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=16),
    )
    return st.one_of(
        st.just(_ABSENT),
        st.dictionaries(keys, _record_override_values, max_size=8),
        _non_dict_record_overrides,
    )


@st.composite
def _prompt_cases(draw):
    modality = draw(st.sampled_from(GEOMETRY_MODALITIES))
    labels = draw(_label_sets)
    overrides = draw(_override_states(labels))
    return SimpleNamespace(modality=modality, labels=labels,
                           overrides=overrides)


@st.composite
def _quarter_safe_box(draw, width, height):
    """An in-bounds positive box on the quarter-pixel lattice: every
    coordinate is n/4 (exact in binary floating point), so the bounds
    checks survive the JSON round trips with no rounding slop."""
    l4 = draw(st.integers(min_value=0, max_value=4 * width - 1))
    w4 = draw(st.integers(min_value=1, max_value=4 * width - l4))
    t4 = draw(st.integers(min_value=0, max_value=4 * height - 1))
    h4 = draw(st.integers(min_value=1, max_value=4 * height - t4))
    return {"left": l4 / 4, "top": t4 / 4, "width": w4 / 4, "height": h4 / 4}


@st.composite
def _valid_worker_payloads(draw, modality, labels):
    """A valid worker response for `modality`: 0..4 regions with
    in-Label_Set classes, RLE / quarter-lattice box geometry, and a
    score that is independently absent, null, or numeric per region."""
    width = draw(st.integers(min_value=1, max_value=4000))
    height = draw(st.integers(min_value=1, max_value=4000))
    regions = []
    for _ in range(draw(st.integers(min_value=0, max_value=4))):
        cls = draw(st.sampled_from(labels))
        if modality == "Segmentation":
            region = {"class": cls, "rle": draw(_rle_strings)}
        else:
            region = {"class": cls,
                      "box": draw(_quarter_safe_box(width, height))}
        score_arm = draw(st.sampled_from(("absent", "null", "number")))
        if score_arm == "null":
            region["score"] = None
        elif score_arm == "number":
            region["score"] = draw(_scores)
        regions.append(region)
    return {"regions": regions,
            "image_width": width, "image_height": height}


_ENVELOPE_KINDS = ("payload_not_object", "regions_missing",
                   "regions_not_list", "bad_dims")
_SEG_REGION_KINDS = ("region_not_dict", "class_out_of_set", "missing_rle")
_OD_REGION_KINDS = ("region_not_dict", "class_out_of_set", "missing_box",
                    "bad_geometry_type", "degenerate_box",
                    "negative_origin", "out_of_bounds_box")


def _draw_valid_region(draw, modality, labels, width, height):
    """Valid filler around an offending region."""
    cls = draw(st.sampled_from(labels))
    if modality == "Segmentation":
        region = {"class": cls, "rle": draw(_rle_strings)}
    else:
        region = {"class": cls,
                  "box": draw(_quarter_safe_box(width, height))}
    if draw(st.booleans()):
        region["score"] = draw(_scores)
    return region


@st.composite
def _invalid_worker_payloads(draw, modality, labels):
    """A worker response the consumer's rules reject, enumerated by kind
    (the test_property_grounded_sam_consumer.py Property 12 space, minus
    the transport arms Property 7 owns separately)."""
    kinds = _ENVELOPE_KINDS + (_SEG_REGION_KINDS
                               if modality == "Segmentation"
                               else _OD_REGION_KINDS)
    kind = draw(st.sampled_from(kinds))

    if kind == "payload_not_object":
        return draw(st.sampled_from(
            [None, True, 7, 2.5, "regions", [], ["regions"]]))

    width = draw(st.integers(min_value=1, max_value=2000))
    height = draw(st.integers(min_value=1, max_value=2000))

    if kind == "regions_missing":
        payload = {"image_width": width, "image_height": height}
        if draw(st.booleans()):
            payload["regions"] = None       # explicit null, same failure
        return payload
    if kind == "regions_not_list":
        return {"regions": draw(st.sampled_from([{}, "regions", 5])),
                "image_width": width, "image_height": height}
    if kind == "bad_dims":
        payload = {"regions": [],
                   "image_width": width, "image_height": height}
        dim = draw(st.sampled_from(("image_width", "image_height")))
        arm = draw(st.sampled_from(
            ("missing", "null", "string", "float", "list")))
        if arm == "missing":
            del payload[dim]
        else:
            payload[dim] = {"null": None, "string": "640",
                            "float": draw(st.sampled_from([12.5, 640.0])),
                            "list": []}[arm]
        return payload

    # ---------------- region-level kinds: valid envelope, one offender
    ok_class = draw(st.sampled_from(labels))
    if kind == "region_not_dict":
        bad = draw(st.sampled_from(["region", 7, None, True, ["class"]]))
    elif kind == "class_out_of_set":
        arm = draw(st.sampled_from(("outside", "missing", "null",
                                    "non_string")))
        if modality == "Segmentation":
            bad = {"rle": draw(_rle_strings)}
        else:
            bad = {"box": draw(_quarter_safe_box(width, height))}
        if arm == "outside":
            bad["class"] = draw(
                st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=24)
                .filter(lambda name: name not in labels))
        elif arm == "null":
            bad["class"] = None
        elif arm == "non_string":
            bad["class"] = 7
        # "missing": no class key at all
    elif kind == "missing_rle":
        arm = draw(st.sampled_from(("absent", "empty", "null")))
        bad = {"class": ok_class}
        if arm == "empty":
            bad["rle"] = ""
        elif arm == "null":
            bad["rle"] = None
        if draw(st.booleans()):
            # The other modality's geometry is not this modality's.
            bad["box"] = draw(_quarter_safe_box(width, height))
    elif kind == "missing_box":
        arm = draw(st.sampled_from(("absent", "non_dict")))
        bad = {"class": ok_class}
        if arm == "non_dict":
            bad["box"] = draw(st.sampled_from(
                ["10,20,30,40", 7, None, [10.0, 20.0, 30.0, 40.0], True]))
        elif draw(st.booleans()):
            bad["rle"] = "12 5 3 5"     # decoy: the wrong geometry kind
    elif kind == "bad_geometry_type":
        box = draw(_quarter_safe_box(width, height))
        field = draw(st.sampled_from(("left", "top", "width", "height")))
        if draw(st.booleans()):
            box[field] = draw(st.sampled_from(
                [None, "12", [], {}, True, False]))
        else:
            del box[field]
        bad = {"class": ok_class, "box": box}
    elif kind == "degenerate_box":
        box = draw(_quarter_safe_box(width, height))
        side = draw(st.sampled_from(("width", "height")))
        box[side] = draw(st.sampled_from([0, -1, -12, 0.0]))
        bad = {"class": ok_class, "box": box}
    elif kind == "negative_origin":
        box = draw(_quarter_safe_box(width, height))
        corner = draw(st.sampled_from(("left", "top")))
        box[corner] = draw(st.sampled_from([-0.25, -1, -500]))
        bad = {"class": ok_class, "box": box}
    else:  # out_of_bounds_box — integer arithmetic, strictly past a bound
        axis = draw(st.sampled_from(("horizontal", "vertical")))
        if axis == "horizontal":
            left = draw(st.integers(min_value=0, max_value=width))
            box = {"left": left, "top": 0,
                   "width": width - left + draw(st.integers(1, 10)),
                   "height": height}
        else:
            top = draw(st.integers(min_value=0, max_value=height))
            box = {"left": 0, "top": top, "width": width,
                   "height": height - top + draw(st.integers(1, 10))}
        bad = {"class": ok_class, "box": box}

    regions = [_draw_valid_region(draw, modality, labels, width, height)
               for _ in range(draw(st.integers(min_value=0, max_value=2)))]
    regions.append(bad)
    if draw(st.booleans()):
        regions.append(
            _draw_valid_region(draw, modality, labels, width, height))
    return {"regions": regions,
            "image_width": width, "image_height": height}


@st.composite
def _response_cases(draw):
    modality = draw(st.sampled_from(GEOMETRY_MODALITIES))
    labels = draw(_label_sets)
    if draw(st.booleans()):
        payload = draw(_valid_worker_payloads(modality, labels))
    else:
        payload = draw(_invalid_worker_payloads(modality, labels))
    return SimpleNamespace(modality=modality, labels=labels,
                           payload=payload)


_FAILURE_KINDS = ("invoke_exception", "invoke_timeout", "function_error",
                  "non_json", "invalid_payload", "presign_failure")


@st.composite
def _failure_cases(draw):
    """One categorized per-sample outcome among succeeding siblings."""
    modality = draw(st.sampled_from(GEOMETRY_MODALITIES))
    labels = draw(_label_sets)
    sample_count = draw(st.integers(min_value=1, max_value=3))
    failing_index = draw(st.integers(min_value=0,
                                     max_value=sample_count - 1))
    kind = draw(st.sampled_from(_FAILURE_KINDS))
    detail = None
    outcome = None
    if kind == "invoke_exception":
        detail = draw(_ascii_details)
        outcome = {"raise": RuntimeError(detail)}
    elif kind == "invoke_timeout":
        exc_type = draw(st.sampled_from((ReadTimeoutError,
                                         ConnectTimeoutError)))
        outcome = {"raise": exc_type(endpoint_url="https://lambda.test/g")}
    elif kind == "function_error":
        detail = draw(_ascii_details)
        outcome = {"function_error": detail}
    elif kind == "non_json":
        outcome = {"raw_payload": draw(st.sampled_from(
            [b"", b"not json", b'{"regions": [',
             b"\xff\xfe\x00\x01binary"]))}
    elif kind == "invalid_payload":
        outcome = {"payload": draw(_invalid_worker_payloads(modality,
                                                            labels))}
    return SimpleNamespace(modality=modality, labels=labels,
                           sample_count=sample_count,
                           failing_index=failing_index, kind=kind,
                           detail=detail, outcome=outcome)


@st.composite
def _termination_cases(draw):
    """A sample count and a remaining-time profile: no context at all,
    a profile that never trips the guard, or one that exhausts before
    sample `trip_at` (0 = before the first sample)."""
    sample_count = draw(st.integers(min_value=1, max_value=5))
    modality = draw(st.sampled_from(GEOMETRY_MODALITIES))
    arm = draw(st.sampled_from(("no_context", "never_trips", "trips")))
    trip_at = None
    profile = None
    if arm == "never_trips":
        profile = draw(st.lists(
            st.integers(min_value=DEADLINE_GUARD_MILLIS,
                        max_value=900_000),
            min_size=1, max_size=sample_count))
    elif arm == "trips":
        trip_at = draw(st.integers(min_value=0,
                                   max_value=sample_count - 1))
        highs = draw(st.lists(
            st.integers(min_value=DEADLINE_GUARD_MILLIS,
                        max_value=900_000),
            min_size=trip_at, max_size=trip_at))
        low = draw(st.integers(min_value=0,
                               max_value=DEADLINE_GUARD_MILLIS - 1))
        profile = highs + [low]
    return SimpleNamespace(sample_count=sample_count, modality=modality,
                           arm=arm, trip_at=trip_at, profile=profile)


@st.composite
def _pipeline_state_cases(draw):
    """A run whose samples mix success, worker failure, and rejected
    payloads."""
    modality = draw(st.sampled_from(GEOMETRY_MODALITIES))
    labels = draw(_label_sets)
    conditions = draw(st.lists(
        st.sampled_from(("ok", "function_error", "invalid_payload")),
        min_size=1, max_size=3))
    outcomes = []
    for condition in conditions:
        if condition == "ok":
            outcomes.append(
                {"payload": draw(_valid_worker_payloads(modality, labels))})
        elif condition == "function_error":
            outcomes.append({"function_error": "worker exploded"})
        else:
            outcomes.append(
                {"payload": draw(_invalid_worker_payloads(modality,
                                                          labels))})
    return SimpleNamespace(modality=modality, labels=labels,
                           conditions=conditions, outcomes=outcomes)


# =========================================================================== #
# Property 5
# =========================================================================== #

class TestProperty5InvokePayloadEqualsConsumerDerivation:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_prompt_cases())
    @example(case=SimpleNamespace(       # surviving + blank + extra key mix
        modality="Segmentation", labels=["dent", "scratch"],
        overrides={"dent": " gap between broken pieces ", "scratch": "   ",
                   "renamed-away": "stale"}))
    @example(case=SimpleNamespace(       # non-dict recorded attribute
        modality="ObjectDetection", labels=["dent"],
        overrides="not-a-map"))
    @example(case=SimpleNamespace(       # override-free run: no attribute
        modality="ObjectDetection", labels=["dent", "scratch"],
        overrides=_ABSENT))
    def test_property_invoke_payload_equals_consumer_derivation(
            self, genv, case):
        """
        **Feature: grounded-sam-prompt-tuning-preview, Property 5: The
        executor's invoke payload equals the consumer's derivation**

        Each Sample_Image invocation carries `prompts` equal to
        `dda_autolabel_worker._grounded_sam_prompts(label_set,
        overrides)` — the consumer's function, called as the oracle —
        the run's task type as `modality`, and an https presigned URL
        for the sample's own object (Req 2.2, 4.1); the derivation reads
        the run's own recorded entries (Req 7.1).

        **Validates: Requirements 2.2, 4.1, 7.1**
        """
        run = genv.seed_run(case.modality, case.labels, 1,
                            overrides=case.overrides)
        fake = genv.use_preview_client([{"payload": VALID_EMPTY}])

        outcome = genv.execute(run)

        assert outcome["status"] == "Completed"
        assert len(fake.invocations) == 1
        invocation = fake.invocations[0]
        assert invocation["FunctionName"] == GS_FUNCTION
        assert invocation["InvocationType"] == "RequestResponse"

        sent = json.loads(invocation["Payload"])
        assert set(sent) == {"image_s3_presigned_url", "prompts",
                             "modality"}
        recorded = None if case.overrides is _ABSENT else case.overrides
        assert sent["prompts"] == genv.worker._grounded_sam_prompts(
            case.labels, recorded)
        assert sent["modality"] == case.modality
        url = sent["image_s3_presigned_url"]
        assert isinstance(url, str) and url.startswith("https://")
        assert DATASET_BUCKET in url
        assert run.sample_keys[0] in url


# =========================================================================== #
# Property 6
# =========================================================================== #

class TestProperty6ResponseAcceptanceMirrorsConsumer:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_response_cases())
    @example(case=SimpleNamespace(       # empty-regions success (Req 4.4)
        modality="Segmentation", labels=["dent"],
        payload={"regions": [], "image_width": 64, "image_height": 48}))
    @example(case=SimpleNamespace(       # empty-regions success (OD)
        modality="ObjectDetection", labels=["dent"],
        payload={"regions": [], "image_width": 64, "image_height": 48}))
    @example(case=SimpleNamespace(       # out-of-set class rejection
        modality="Segmentation", labels=["dent"],
        payload={"regions": [{"class": "crack", "rle": "12 5 3 5"}],
                 "image_width": 64, "image_height": 48}))
    def test_property_acceptance_and_payload_mirror_the_consumer(
            self, genv, case):
        """
        **Feature: grounded-sam-prompt-tuning-preview, Property 6:
        Response acceptance and payload construction mirror the
        consumer's validation**

        The sample resolves Succeeded exactly when the consumer's real
        `_generate_grounded_sam_prelabel` accepts the identical payload
        (Req 4.2); on success the written prelabel carries the renderer
        shapes with `score` present exactly when the worker returned one
        (Req 4.3), an empty response resolving as Succeeded with an
        empty Pre_Label (Req 4.4).

        **Validates: Requirements 4.2, 4.3, 4.4**
        """
        accepted, consumer_prelabel = genv.oracle(
            case.modality, case.labels, case.payload)

        run = genv.seed_run(case.modality, case.labels, 1)
        genv.use_preview_client([{"payload": case.payload}])
        outcome = genv.execute(run)

        assert outcome["status"] == "Completed"
        item = genv.sample_items(run)[0]

        if not accepted:
            assert item["state"] == "Failed"
            assert item["failure_category"] == "model_error"
            assert item["failure_reason"]
            assert genv.payload(run, 0)["state"] == "Failed"
            return

        assert item["state"] == "Succeeded"
        assert "failure_category" not in item

        # The consumer's own result is the expected prelabel, with the
        # one declared divergence applied: ObjectDetection boxes carry
        # the worker's score for display (the consumer drops it from
        # the stored job shape); Segmentation regions already keep it.
        expected_prelabel = dict(consumer_prelabel)
        if case.modality == "ObjectDetection":
            boxes = []
            for stored_box, worker_region in zip(
                    consumer_prelabel["boxes"], case.payload["regions"]):
                box = dict(stored_box)
                if worker_region.get("score") is not None:
                    box["score"] = worker_region["score"]
                boxes.append(box)
            expected_prelabel["boxes"] = boxes

        payload = genv.payload(run, 0)
        assert payload == {
            "sample_key": run.sample_keys[0],
            "state": "Succeeded",
            "prelabel": expected_prelabel,
            "image_width": case.payload["image_width"],
            "image_height": case.payload["image_height"],
        }

        # Score presence is exactly the worker's, entry by entry.
        entries = payload["prelabel"][
            "regions" if case.modality == "Segmentation" else "boxes"]
        assert len(entries) == len(case.payload["regions"])
        for entry, worker_region in zip(entries, case.payload["regions"]):
            assert (("score" in entry)
                    == (worker_region.get("score") is not None))


# =========================================================================== #
# Property 7
# =========================================================================== #

def _expected_failure(case, sample_key):
    """(category, [reason substrings]) for the generated outcome."""
    if case.kind == "invoke_exception":
        return "model_error", ["Grounded-SAM worker invocation failed",
                               case.detail]
    if case.kind == "invoke_timeout":
        return "timeout", ["timed out after 240s"]
    if case.kind == "function_error":
        return "model_error", ["Grounded-SAM worker failed: ", case.detail]
    if case.kind == "non_json":
        return "model_error", ["unparseable output"]
    if case.kind == "invalid_payload":
        return "model_error", []
    return "image_access_failure", [
        f"s3://{DATASET_BUCKET}/{sample_key}", "could not be presigned"]


class _SelectivePresignBreaker:
    """Wraps a real S3 client; presigning exactly one key raises."""

    def __init__(self, inner, bad_key):
        self._inner = inner
        self._bad_key = bad_key

    def generate_presigned_url(self, operation, Params=None, **kwargs):
        if Params and Params.get("Key") == self._bad_key:
            raise RuntimeError("presign refused by the test seam")
        return self._inner.generate_presigned_url(operation,
                                                  Params=Params, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TestProperty7FailureCategorizationIsTotal:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_failure_cases())
    def test_property_failure_categorization_total_with_detail(
            self, genv, case):
        """
        **Feature: grounded-sam-prompt-tuning-preview, Property 7:
        Failure categorization is total, one existing category per
        outcome, worker detail carried**

        Every generated outcome resolves its sample Failed with exactly
        one member of the existing `PREVIEW_FAILURE_CATEGORIES` —
        `timeout` for timed-out invocations, `image_access_failure` for
        presign failures, `model_error` otherwise — the reason carrying
        the outcome's detail (Req 4.5, 4.6), and sibling samples resolve
        independently with the run reaching Completed.

        **Validates: Requirements 4.5, 4.6**
        """
        run = genv.seed_run(case.modality, case.labels, case.sample_count)
        failing_key = run.sample_keys[case.failing_index]

        # Scripted outcomes in invocation order; a presign failure never
        # reaches the worker, so its slot has no outcome at all.
        outcomes = [{"payload": VALID_EMPTY}] * case.sample_count
        if case.kind == "presign_failure":
            outcomes.pop()
        else:
            outcomes[case.failing_index] = case.outcome
        fake = genv.use_preview_client(outcomes)

        saved_factory = genv.module.get_s3_client_for_bucket
        if case.kind == "presign_failure":
            def breaking_factory(usecase, bucket,
                                 session_name="portal-s3-access"):
                return _SelectivePresignBreaker(
                    saved_factory(usecase, bucket, session_name),
                    failing_key)
            genv.module.get_s3_client_for_bucket = breaking_factory
        try:
            outcome = genv.execute(run)
        finally:
            genv.module.get_s3_client_for_bucket = saved_factory

        assert outcome["status"] == "Completed"
        assert outcome["failed"] == 1
        assert outcome["succeeded"] == case.sample_count - 1

        expected_category, reason_parts = _expected_failure(case,
                                                            failing_key)
        items = genv.sample_items(run)
        for index, item in enumerate(items):
            if index == case.failing_index:
                assert item["state"] == "Failed"
                # Exactly one category, from the existing closed set.
                assert item["failure_category"] == expected_category
                assert (item["failure_category"]
                        in genv.module.PREVIEW_FAILURE_CATEGORIES)
                assert item["failure_reason"]
                for part in reason_parts:
                    assert part in item["failure_reason"]
                payload = genv.payload(run, index)
                assert payload["state"] == "Failed"
                assert payload["failure_category"] == expected_category
                assert payload["failure_reason"] == item["failure_reason"]
            else:
                # Siblings resolve independently (Req 4.5).
                assert item["state"] == "Succeeded"
                assert "failure_category" not in item

        # A presign failure invokes nothing for its sample; every other
        # outcome is exactly one invocation for it.
        expected_invocations = (case.sample_count - 1
                                if case.kind == "presign_failure"
                                else case.sample_count)
        assert len(fake.invocations) == expected_invocations
        invoked_keys = [key for key in run.sample_keys
                        if not (case.kind == "presign_failure"
                                and key == failing_key)]
        for invocation, key in zip(fake.invocations, invoked_keys):
            assert key in json.loads(
                invocation["Payload"])["image_s3_presigned_url"]


# =========================================================================== #
# Property 8
# =========================================================================== #

class TestProperty8EveryRunTerminates:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_termination_cases())
    @example(case=SimpleNamespace(       # exhausted before the first sample
        sample_count=5, modality="Segmentation", arm="trips", trip_at=0,
        profile=[DEADLINE_GUARD_MILLIS - 1]))
    @example(case=SimpleNamespace(       # exhausted mid-run
        sample_count=3, modality="ObjectDetection", arm="trips", trip_at=1,
        profile=[900_000, 100_000]))
    def test_property_every_grounded_sam_run_terminates(self, genv, case):
        """
        **Feature: grounded-sam-prompt-tuning-preview, Property 8: Every
        grounded-sam run terminates: deadline-guarded samples resolve as
        timeout without invocation, the run reaches Completed, the lock
        is released**

        Samples whose slot cannot fit another 240-second invocation
        resolve as `timeout` failures with zero worker invocations and
        the deadline reason (Req 4.7); the run reaches Completed —
        including an all-guarded run — and the in-flight lock is absent
        on every terminal path (Req 4.8).

        **Validates: Requirements 4.7, 4.8**
        """
        run = genv.seed_run(case.modality, ["dent"], case.sample_count)
        genv.claim_lock(run)
        fake = genv.use_preview_client([{"payload": VALID_EMPTY}])

        context = (None if case.arm == "no_context"
                   else ScriptedContext(case.profile))
        outcome = genv.execute(run, context=context)

        invoked = (case.sample_count if case.arm != "trips"
                   else case.trip_at)

        assert outcome["status"] == "Completed"
        assert outcome["succeeded"] == invoked
        assert outcome["failed"] == case.sample_count - invoked
        assert genv.run_item(run)["status"] == "Completed"

        items = genv.sample_items(run)
        assert len(items) == case.sample_count
        for index, item in enumerate(items):
            if index < invoked:
                assert item["state"] == "Succeeded"
            else:
                # Deadline-guarded: timeout, the deadline reason, and a
                # written payload — a resolution, never a stranded item.
                assert item["state"] == "Failed"
                assert item["failure_category"] == "timeout"
                assert item["failure_reason"] == DEADLINE_REASON
                payload = genv.payload(run, index)
                assert payload["state"] == "Failed"
                assert payload["failure_category"] == "timeout"

        # Zero invocations for guarded samples: the total is exactly the
        # pre-trip count, and each invocation presigned its own sample.
        assert len(fake.invocations) == invoked
        for invocation, key in zip(fake.invocations,
                                   run.sample_keys[:invoked]):
            assert key in json.loads(
                invocation["Payload"])["image_s3_presigned_url"]

        # The guard consults the context once per sample until it
        # latches, then never again.
        if case.arm == "never_trips":
            assert context.calls == case.sample_count
        elif case.arm == "trips":
            assert context.calls == case.trip_at + 1

        # The in-flight lock is released on the terminal path.
        assert genv.lock_item(run) is None


# =========================================================================== #
# Property 10
# =========================================================================== #

class TestProperty10NoLabelingPipelineState:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_pipeline_state_cases())
    @example(case=SimpleNamespace(       # all three outcome kinds in one run
        modality="Segmentation", labels=["dent"],
        conditions=["ok", "function_error", "invalid_payload"],
        outcomes=[
            {"payload": {"regions": [{"class": "dent", "rle": "1 2 3"}],
                         "image_width": 64, "image_height": 48}},
            {"function_error": "worker exploded"},
            {"payload": {"regions": "nope",
                         "image_width": 64, "image_height": 48}},
        ]))
    def test_property_runs_create_no_labeling_pipeline_state(
            self, genv, case):
        """
        **Feature: grounded-sam-prompt-tuning-preview, Property 10:
        Grounded-sam runs create no labeling-pipeline state**

        After a mixed-outcome run: the Labeling_Job table is unchanged,
        every new tasks-table item belongs to the run's own `PREVIEW#`
        namespace (no Task_Assignment item, nothing a labeler could
        reach through `assignee-index`), nothing exists under
        `labeling/{usecase_id}/`, and the run's payloads are the only
        new artifacts — all under `labeling-previews/` (Req 4.9, 9.3).

        **Validates: Requirements 4.9, 9.3**
        """
        sample_count = len(case.conditions)

        job_baseline = genv.job_ids()
        task_baseline = genv.task_keys()
        artifact_baseline = genv.artifact_keys()
        pipeline_prefix = f"labeling/{genv.usecase_id}/"

        run = genv.seed_run(case.modality, case.labels, sample_count)
        genv.use_preview_client(case.outcomes)
        outcome = genv.execute(run)

        assert outcome["status"] == "Completed"
        expected_failed = sum(1 for condition in case.conditions
                              if condition != "ok")
        assert outcome["failed"] == expected_failed
        assert outcome["succeeded"] == sample_count - expected_failed

        # No Labeling_Job record.
        assert genv.job_ids() == job_baseline

        # No Task_Assignment item: every new tasks-table item is the
        # run's own RUN / IMAGE#{i} preview item, and none of them
        # carries assignee_user_id, so nothing projects into
        # assignee-index and no labeler API can ever see one.
        run_pk = f"PREVIEW#{run.run_id}"
        expected_task_keys = {(run_pk, "RUN")} | {
            (run_pk, f"IMAGE#{index:03d}")
            for index in range(sample_count)}
        assert genv.task_keys() - task_baseline == expected_task_keys
        for item in [genv.run_item(run)] + genv.sample_items(run):
            assert "assignee_user_id" not in item

        # No pipeline Pre_Label artifact; the run's payloads exist only
        # under the ephemeral labeling-previews/ prefix.
        assert genv.artifact_keys(pipeline_prefix) == set()
        expected_payload_keys = {
            (f"labeling-previews/{genv.usecase_id}/"
             f"{run.run_id}/{index}.json")
            for index in range(sample_count)}
        assert (genv.artifact_keys() - artifact_baseline
                == expected_payload_keys)
