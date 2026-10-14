"""
Grounded-SAM consumer property tests.

Spec: grounded-sam-autolabel, task 3.3.

Three properties over the real `_generate_grounded_sam_prelabel` path in
dda_autolabel_worker.py, driven end-to-end through `handler` against the
moto-backed stack (conftest.py tables + artifacts bucket, real
shared_utils cross-account fallback) with a fake Lambda client injected
at the module's `grounded_sam_lambda_client` test injection point (the
test_dda_autolabel_worker.py FakeSamLambdaClient pattern), 100 Hypothesis
examples per property:

**Feature: grounded-sam-autolabel, Property 10: The consumer's invocation
payload carries the presigned URL, the exact Prompt_Map, and the
modality** — *For any* Label_Set and *any* job-record override map,
processing a `grounded-sam` message SHALL invoke the worker exactly once
with a payload whose `image_s3_presigned_url` is an https URL, whose
`prompts` equals the Property 5 Prompt_Map for that Label_Set and
override map, and whose `modality` equals the message's modality.
**Validates: Requirements 4.1**

**Feature: grounded-sam-autolabel, Property 11: Valid worker responses
map to the exact stored Pre_Label shapes** — *For any* valid worker
response (regions with in-Label_Set classes; Segmentation regions
carrying `rle` and optional `score`; ObjectDetection regions carrying
in-bounds positive `box` geometry and `score`; including the
empty-regions response), the stored Pre_Label SHALL be `{modality,
regions: [{class, rle, score?}], image_width, image_height}` for
Segmentation (classes and scores preserved) and `{modality, boxes:
[{class, left, top, width, height}], image_width, image_height}` for
ObjectDetection (exact key set, float geometry, score dropped — the
Bedrock shape), and the task SHALL resolve Available with the artifact
written. **Validates: Requirements 4.6, 4.7**

**Feature: grounded-sam-autolabel, Property 12: Invalid worker responses
fail the image without an artifact** — *For any* invalid worker outcome
— a function error, an unparseable payload, a missing or non-list
`regions`, non-integer dimensions, a region whose class is outside the
Label_Set, a Segmentation region without `rle`, an ObjectDetection
region without a `box` or with non-positive or out-of-bounds geometry —
the consumer SHALL mark the task's Pre_Label generation Failed with a
descriptive reason and SHALL write no Pre_Label artifact.
**Validates: Requirements 4.4, 4.5**

Oracles
-------
Every oracle is restated in this file, never imported from the code under
test:

- Property 10's Prompt_Map oracle restates Requirement 2.7: one
  `{label, prompt}` pair per Label_Set label in Label_Set order, the
  prompt being the label's override exactly when that override is a
  string non-empty after trimming, the label name otherwise (any other
  override shape — absent key, explicit null, non-dict record value,
  non-string entry — degrades to the label-name fallback).
- Property 11's stored-shape oracle restates the design's data model
  verbatim and is asserted by whole-document deep equality, so the exact
  key sets are pinned both ways; ObjectDetection geometry is additionally
  type-checked as floats (the Bedrock shape drops the worker's `score`).
  ObjectDetection boxes are generated on a quarter-pixel integer lattice
  (`n / 4`), which binary floating point represents exactly, so the
  in-bounds constraint `left + width <= image_width` survives the two
  JSON round trips (fake client → consumer → stored artifact) with no
  rounding slop.
- Property 12's generators enumerate the invalid-outcome space by kind
  (function error; non-JSON bytes; non-object payloads; missing/non-list
  `regions`; missing/typed-wrong dimensions; region entries that are not
  objects, carry an out-of-Label_Set class, lack their modality's
  geometry — optionally carrying the *other* modality's geometry as a
  decoy — or carry non-numeric/boolean, non-positive, negative-origin,
  or out-of-bounds box geometry). A failed image must end Failed with a
  non-empty reason, no `prelabel_s3_key`, no artifact object, and no
  batch item failure (generation failures never poison the batch).

Harness reuse (Hypothesis cannot consume function-scoped fixtures): the
module-scoped `genv` fixture imports the real worker module inside the
moto mock (the test_dda_autolabel_worker.py `worker` fixture convention)
and per-example jobs/tasks are created inside the test bodies with
uuid-fresh ids, so examples never interfere.
"""
import io
import json
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

REGION = "us-east-1"
DATASET_BUCKET = "test-gsam-consumer-data"
ARTIFACTS_BUCKET = "test-portal-artifacts"
GS_FUNCTION = "test-dda-grounded-sam-worker"

GEOMETRY_MODALITIES = ("Segmentation", "ObjectDetection")

# Sentinel distinguishing "no prompt_overrides key on the job record"
# (the pre-feature record shape) from every explicit value incl. None.
_ABSENT = object()


# ------------------------------------------------------------- fake client

class FakeGroundedSamLambdaClient:
    """Records synchronous Grounded-SAM worker invocations; returns a
    canned payload (JSON-encoded `payload`, verbatim `raw_payload`
    bytes, or a Lambda `FunctionError`) — the FakeSamLambdaClient
    pattern extended with a raw-bytes arm for unparseable outputs."""

    def __init__(self, payload=None, raw_payload=None, function_error=None):
        self.invocations = []
        self.payload = payload
        self.raw_payload = raw_payload
        self.function_error = function_error

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        if self.function_error:
            return {
                "StatusCode": 200,
                "FunctionError": "Unhandled",
                "Payload": io.BytesIO(json.dumps(
                    {"errorMessage": self.function_error}).encode()),
            }
        if self.raw_payload is not None:
            body = self.raw_payload
        else:
            body = json.dumps(self.payload).encode()
        return {"StatusCode": 200, "Payload": io.BytesIO(body)}


# ------------------------------------------------------------------ harness

class ConsumerEnv:
    """Monkeypatch-free slice of test_dda_autolabel_worker.AutolabelEnv:
    one use case + one dataset image shared by every example; per-example
    grounded-sam jobs/tasks; fake-client injection through the module's
    `grounded_sam_lambda_client` test injection point."""

    def __init__(self, stack, worker):
        self.stack = stack
        self.worker = worker
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        # Single-account use case: root cross_account_role_arn makes
        # get_s3_client_for_bucket fall back to default (moto) creds.
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Grounded-SAM Consumer Property Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })
        # The grounded-sam consumer never reads the image bytes (the
        # worker fetches them through the presigned URL), so any body
        # will do.
        image_key = f"imgs/{uuid.uuid4()}.png"
        self.s3.put_object(Bucket=DATASET_BUCKET, Key=image_key,
                           Body=b"\x89PNG-bytes-never-read-by-the-consumer")
        self.image_uri = f"s3://{DATASET_BUCKET}/{image_key}"

    # ------------------------------------------------------------ setup
    def make_job(self, modality, label_set, overrides=_ABSENT):
        job_id = f"labeling-{uuid.uuid4().hex[:12]}"
        auto_label = {"enabled": True, "model": "grounded-sam"}
        if overrides is not _ABSENT:
            auto_label["prompt_overrides"] = overrides
        self.stack.tables.labeling_jobs.put_item(Item={
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": f"job-{job_id}",
            "labeling_backend": "DDA",
            "status": "InProgress",
            "task_type": modality,
            "label_set": label_set,
            "skip_verification": False,
            "auto_label": auto_label,
            "created_at": 1,
            "updated_at": 1,
        })
        return job_id

    def make_task(self, job_id):
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        self.stack.tables.labeling_tasks.put_item(Item={
            "job_id": job_id,
            "task_id": task_id,
            "usecase_id": self.usecase_id,
            "image_s3_uri": self.image_uri,
            "assignee_user_id": "AUTO",
            "status": "Assigned",
            "prelabel_status": "Pending",
        })
        return task_id

    def use_worker_client(self, **kwargs):
        fake = FakeGroundedSamLambdaClient(**kwargs)
        self.worker.grounded_sam_lambda_client = fake
        return fake

    # ------------------------------------------------------------ invoke
    def run_one(self, job_id, task_id, modality, label_set):
        record = {
            "messageId": f"msg-{uuid.uuid4().hex[:8]}",
            "body": json.dumps({
                "job_id": job_id,
                "task_id": task_id,
                "image_s3_uri": self.image_uri,
                "modality": modality,
                "label_set": label_set,
                "model": "grounded-sam",
            }),
        }
        return self.worker.handler({"Records": [record]}, None)

    # ------------------------------------------------------------- store
    def get_task(self, job_id, task_id):
        return self.stack.tables.labeling_tasks.get_item(
            Key={"job_id": job_id, "task_id": task_id}).get("Item")

    def _prelabel_key(self, job_id, task_id):
        return (f"labeling/{self.usecase_id}/{job_id}/prelabels/"
                f"{task_id}.json")

    def prelabel_json(self, job_id, task_id):
        body = self.s3.get_object(
            Bucket=ARTIFACTS_BUCKET,
            Key=self._prelabel_key(job_id, task_id))["Body"].read()
        return json.loads(body)

    def prelabel_exists(self, job_id, task_id):
        try:
            self.s3.head_object(Bucket=ARTIFACTS_BUCKET,
                                Key=self._prelabel_key(job_id, task_id))
            return True
        except Exception:
            return False


@pytest.fixture(scope="module")
def genv(aws_stack):
    """The real dda_autolabel_worker imported inside the moto mock with
    the Grounded-SAM worker function name configured, wrapped in a
    ConsumerEnv (the test_dda_autolabel_worker `worker` fixture
    convention, incl. displacing a collection-time fake shared_utils)."""
    import os

    shared = sys.modules.get("shared_utils")
    if shared is not None and not hasattr(shared, "get_s3_client_for_bucket"):
        sys.modules.pop("shared_utils")

    os.environ["GROUNDED_SAM_WORKER_FUNCTION_NAME"] = GS_FUNCTION
    sys.modules.pop("dda_autolabel_worker", None)
    import dda_autolabel_worker as worker

    # Module read env at import; make sure the test value stuck.
    worker.GROUNDED_SAM_WORKER_FUNCTION_NAME = GS_FUNCTION

    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=DATASET_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass

    env = ConsumerEnv(aws_stack, worker)
    yield env
    worker.grounded_sam_lambda_client = None


# -------------------------------------------------------------- generators

# Printable unicode without surrogates — the sibling property suites'
# alphabet (Latin, Greek, Cyrillic, CJK punctuation, symbols).
_TEXT_ALPHABET = st.characters(min_codepoint=32, max_codepoint=0x2FFF,
                               blacklist_categories=("Cs",))

# Valid Label_Set names: non-empty, stable under strip (job creation
# persists stripped names, so the record and message carry them
# pre-stripped).
_label_names = st.text(alphabet=_TEXT_ALPHABET, min_size=1,
                       max_size=24).map(str.strip).filter(bool)

_label_sets = st.lists(_label_names, min_size=1, max_size=5, unique=True)

_rle_strings = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=24)

_scores = st.one_of(
    st.floats(min_value=0, max_value=1, allow_nan=False,
              allow_infinity=False),
    st.integers(min_value=0, max_value=1),
)

# Override values a job record can physically carry (DynamoDB has no
# float type and map keys are strings, so the record-level space is
# narrower than Property 5's pure space): strings incl. blank-after-trim
# and unicode, plus the non-string shapes a hand-edited record could
# hold (null, bool, number, list) — all of which must degrade to the
# label-name fallback.
_record_override_values = st.one_of(
    st.text(alphabet=_TEXT_ALPHABET, max_size=48),
    st.sampled_from(["", " ", "  \u00a0 "]),
    st.none(),
    st.booleans(),
    st.integers(min_value=-10**9, max_value=10**9),
    st.lists(st.text(alphabet=_TEXT_ALPHABET, max_size=6), max_size=3),
)

# Entire non-dict prompt_overrides record values (explicit null, a
# string, a number, a bool) — the consumer's Prompt_Map is total over
# them (Req 7.6).
_non_dict_record_overrides = st.sampled_from(
    [None, "not-a-map", 7, True])


def _expected_prompt_map(label_set, overrides):
    """The Prompt_Map oracle, restating Requirement 2.7 (never imported
    from the code under test): the override exactly when it is a string
    non-empty after trimming, the label name otherwise."""
    if not isinstance(overrides, dict):
        overrides = {}
    expected = []
    for label in label_set:
        override = overrides.get(label)
        if isinstance(override, str) and override.strip():
            expected.append({"label": label, "prompt": override})
        else:
            expected.append({"label": label, "prompt": label})
    return expected


@st.composite
def _payload_cases(draw):
    """(modality, label_set, record override state): overrides absent
    (the pre-feature record), a string-keyed map mixing in-Label_Set and
    extra keys with conforming and junk values, or a non-dict value."""
    modality = draw(st.sampled_from(GEOMETRY_MODALITIES))
    labels = draw(_label_sets)
    arm = draw(st.sampled_from(("absent", "map", "non_dict")))
    if arm == "absent":
        overrides = _ABSENT
    elif arm == "non_dict":
        overrides = draw(_non_dict_record_overrides)
    else:
        keys = st.one_of(
            st.sampled_from(labels),
            st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=16),
        )
        overrides = draw(st.dictionaries(keys, _record_override_values,
                                         max_size=8))
    return SimpleNamespace(modality=modality, labels=labels,
                           overrides=overrides)


@st.composite
def _quarter_safe_box(draw, width, height):
    """An in-bounds positive box on the quarter-pixel lattice: every
    coordinate is n/4 (exact in binary floating point), and
    left+width <= width / top+height <= height hold exactly."""
    l4 = draw(st.integers(min_value=0, max_value=4 * width - 1))
    w4 = draw(st.integers(min_value=1, max_value=4 * width - l4))
    t4 = draw(st.integers(min_value=0, max_value=4 * height - 1))
    h4 = draw(st.integers(min_value=1, max_value=4 * height - t4))
    return {"left": l4 / 4, "top": t4 / 4, "width": w4 / 4, "height": h4 / 4}


def _draw_valid_region(draw, modality, labels, width, height):
    """One region the consumer must accept, used as filler around an
    offending region in Property 12's generators."""
    cls = draw(st.sampled_from(labels))
    if modality == "Segmentation":
        region = {"class": cls, "rle": draw(_rle_strings)}
        if draw(st.booleans()):
            region["score"] = draw(_scores)
        return region
    region = {"class": cls, "box": draw(_quarter_safe_box(width, height))}
    if draw(st.booleans()):
        region["score"] = draw(_scores)
    return region


@st.composite
def _valid_response_cases(draw):
    """A valid worker response for either modality, with its expected
    stored regions/boxes computed by the restated mapping oracle."""
    modality = draw(st.sampled_from(GEOMETRY_MODALITIES))
    labels = draw(_label_sets)
    width = draw(st.integers(min_value=1, max_value=4000))
    height = draw(st.integers(min_value=1, max_value=4000))
    count = draw(st.integers(min_value=0, max_value=4))
    regions, expected_items = [], []
    for _ in range(count):
        cls = draw(st.sampled_from(labels))
        score_arm = draw(st.sampled_from(("absent", "null", "number")))
        score = draw(_scores) if score_arm == "number" else None
        if modality == "Segmentation":
            rle = draw(_rle_strings)
            region = {"class": cls, "rle": rle}
            entry = {"class": cls, "rle": rle}
            if score_arm == "null":
                region["score"] = None      # dropped like an absent score
            elif score_arm == "number":
                region["score"] = score
                entry["score"] = score      # preserved verbatim
        else:
            box = draw(_quarter_safe_box(width, height))
            region = {"class": cls, "box": box}
            if score_arm == "null":
                region["score"] = None
            elif score_arm == "number":
                region["score"] = score     # always dropped from storage
            entry = {"class": cls, **{k: float(v) for k, v in box.items()}}
        regions.append(region)
        expected_items.append(entry)
    payload = {"regions": regions,
               "image_width": width, "image_height": height}
    return SimpleNamespace(modality=modality, labels=labels, payload=payload,
                           expected_items=expected_items,
                           width=width, height=height)


_ENVELOPE_KINDS = ("function_error", "non_json", "payload_not_object",
                   "regions_missing", "regions_not_list", "bad_dims")
_SEG_REGION_KINDS = ("region_not_dict", "class_out_of_set", "missing_rle")
_OD_REGION_KINDS = ("region_not_dict", "class_out_of_set", "missing_box",
                    "bad_geometry_type", "degenerate_box",
                    "negative_origin", "out_of_bounds_box")


@st.composite
def _invalid_outcome_cases(draw):
    """One invalid worker outcome per Property 12's enumeration, as the
    fake-client construction (`client_kwargs`) that produces it."""
    modality = draw(st.sampled_from(GEOMETRY_MODALITIES))
    labels = draw(_label_sets)
    kinds = _ENVELOPE_KINDS + (_SEG_REGION_KINDS
                               if modality == "Segmentation"
                               else _OD_REGION_KINDS)
    kind = draw(st.sampled_from(kinds))

    def case(client_kwargs):
        return SimpleNamespace(modality=modality, labels=labels, kind=kind,
                               client_kwargs=client_kwargs)

    if kind == "function_error":
        message = draw(st.text(alphabet=_TEXT_ALPHABET,
                               min_size=1, max_size=32))
        return case({"function_error": message})
    if kind == "non_json":
        raw = draw(st.sampled_from(
            [b"", b"not json", b'{"regions": [', b"\xff\xfe\x00\x01binary"]))
        return case({"raw_payload": raw})
    if kind == "payload_not_object":
        payload = draw(st.sampled_from(
            [None, True, 7, 2.5, "regions", [], ["regions"]]))
        return case({"payload": payload})

    width = draw(st.integers(min_value=1, max_value=2000))
    height = draw(st.integers(min_value=1, max_value=2000))

    if kind == "regions_missing":
        payload = {"image_width": width, "image_height": height}
        if draw(st.booleans()):
            payload["regions"] = None       # explicit null, same failure
        return case({"payload": payload})
    if kind == "regions_not_list":
        payload = {"regions": draw(st.sampled_from([{}, "regions", 5, True])),
                   "image_width": width, "image_height": height}
        return case({"payload": payload})
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
        return case({"payload": payload})

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
                .filter(lambda s: s not in labels))
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
            # The other modality's geometry is not this modality's (4.5).
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
    payload = {"regions": regions,
               "image_width": width, "image_height": height}
    return case({"payload": payload})


# --------------------------------------------------------------- Property 10

class TestProperty10InvocationPayload:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_payload_cases())
    @example(case=SimpleNamespace(       # pre-feature record: no key at all
        modality="Segmentation", labels=["dent", "scratch"],
        overrides=_ABSENT))
    @example(case=SimpleNamespace(       # override + blank + extra key mix
        modality="ObjectDetection", labels=["dent", "scratch"],
        overrides={"dent": " small surface dent ", "scratch": "   ",
                   "renamed-away": "stale"}))
    def test_property_invocation_payload_carries_url_prompt_map_modality(
            self, genv, case):
        """
        **Feature: grounded-sam-autolabel, Property 10: The consumer's
        invocation payload carries the presigned URL, the exact
        Prompt_Map, and the modality**

        Exactly one synchronous invocation of the configured worker,
        whose payload carries an https presigned URL for the message's
        image, `prompts` equal to the restated Prompt_Map oracle for the
        message's Label_Set and the job record's override state, and the
        message's modality (Req 4.1).

        **Validates: Requirements 4.1**
        """
        fake = genv.use_worker_client(payload={
            "regions": [], "image_width": 64, "image_height": 48})
        job_id = genv.make_job(case.modality, case.labels,
                               overrides=case.overrides)
        task_id = genv.make_task(job_id)

        result = genv.run_one(job_id, task_id, case.modality, case.labels)

        assert result == {"batchItemFailures": []}
        assert len(fake.invocations) == 1
        invocation = fake.invocations[0]
        assert invocation["FunctionName"] == GS_FUNCTION
        assert invocation["InvocationType"] == "RequestResponse"

        payload = json.loads(invocation["Payload"])
        url = payload["image_s3_presigned_url"]
        assert isinstance(url, str) and url.startswith("https://")
        assert DATASET_BUCKET in url
        record_overrides = (None if case.overrides is _ABSENT
                            else case.overrides)
        assert payload["prompts"] == _expected_prompt_map(
            case.labels, record_overrides)
        assert payload["modality"] == case.modality


# --------------------------------------------------------------- Property 11

class TestProperty11StoredPrelabelShapes:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_valid_response_cases())
    @example(case=SimpleNamespace(       # empty-regions success (Seg)
        modality="Segmentation", labels=["dent"],
        payload={"regions": [], "image_width": 64, "image_height": 48},
        expected_items=[], width=64, height=48))
    @example(case=SimpleNamespace(       # empty-regions success (OD)
        modality="ObjectDetection", labels=["dent"],
        payload={"regions": [], "image_width": 64, "image_height": 48},
        expected_items=[], width=64, height=48))
    def test_property_valid_responses_store_exact_prelabel_shapes(
            self, genv, case):
        """
        **Feature: grounded-sam-autolabel, Property 11: Valid worker
        responses map to the exact stored Pre_Label shapes**

        Segmentation stores `{modality, regions: [{class, rle, score?}],
        image_width, image_height}` with classes and scores preserved;
        ObjectDetection stores `{modality, boxes: [{class, left, top,
        width, height}], image_width, image_height}` — the exact key
        set, float geometry, worker score dropped (the Bedrock shape);
        an empty-regions response is a success. The task resolves
        Available with the artifact written (Req 4.6, 4.7).

        **Validates: Requirements 4.6, 4.7**
        """
        genv.use_worker_client(payload=case.payload)
        job_id = genv.make_job(case.modality, case.labels)
        task_id = genv.make_task(job_id)

        result = genv.run_one(job_id, task_id, case.modality, case.labels)

        assert result == {"batchItemFailures": []}
        task = genv.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Available"
        assert task["prelabel_s3_key"] == (
            f"labeling/{genv.usecase_id}/{job_id}/prelabels/{task_id}.json")

        stored = genv.prelabel_json(job_id, task_id)
        if case.modality == "Segmentation":
            assert stored == {
                "modality": "Segmentation",
                "regions": case.expected_items,
                "image_width": case.width,
                "image_height": case.height,
            }
        else:
            assert stored == {
                "modality": "ObjectDetection",
                "boxes": case.expected_items,
                "image_width": case.width,
                "image_height": case.height,
            }
            for box in stored["boxes"]:
                # The Bedrock shape byte-exactly: this key set and
                # nothing else (no score), all geometry as floats.
                assert set(box) == {"class", "left", "top",
                                    "width", "height"}
                for field in ("left", "top", "width", "height"):
                    assert isinstance(box[field], float)


# --------------------------------------------------------------- Property 12

class TestProperty12InvalidResponsesFailWithoutArtifact:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_invalid_outcome_cases())
    @example(case=SimpleNamespace(       # Lambda FunctionError
        modality="Segmentation", labels=["dent"], kind="function_error",
        client_kwargs={"function_error": "onnx session failed"}))
    @example(case=SimpleNamespace(       # unparseable payload bytes
        modality="ObjectDetection", labels=["dent"], kind="non_json",
        client_kwargs={"raw_payload": b"not json"}))
    @example(case=SimpleNamespace(       # out-of-Label_Set class
        modality="Segmentation", labels=["dent"], kind="class_out_of_set",
        client_kwargs={"payload": {
            "regions": [{"class": "crack", "rle": "12 5 3 5"}],
            "image_width": 64, "image_height": 48}}))
    @example(case=SimpleNamespace(       # out-of-bounds box
        modality="ObjectDetection", labels=["dent"],
        kind="out_of_bounds_box",
        client_kwargs={"payload": {
            "regions": [{"class": "dent",
                         "box": {"left": 60.0, "top": 0.0,
                                 "width": 10.0, "height": 8.0}}],
            "image_width": 64, "image_height": 48}}))
    def test_property_invalid_responses_fail_image_without_artifact(
            self, genv, case):
        """
        **Feature: grounded-sam-autolabel, Property 12: Invalid worker
        responses fail the image without an artifact**

        A function error, unparseable payload, missing/non-list
        `regions`, non-integer dimensions, out-of-Label_Set class,
        missing modality geometry, or degenerate/out-of-bounds box marks
        the task's Pre_Label generation Failed with a descriptive
        reason, writes no Pre_Label artifact, and reports no batch item
        failure (Req 4.4, 4.5).

        **Validates: Requirements 4.4, 4.5**
        """
        genv.use_worker_client(**case.client_kwargs)
        job_id = genv.make_job(case.modality, case.labels)
        task_id = genv.make_task(job_id)

        result = genv.run_one(job_id, task_id, case.modality, case.labels)

        # A generation failure is absorbed per record, never a batch
        # item failure.
        assert result == {"batchItemFailures": []}
        task = genv.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Failed", case.kind
        error = task.get("prelabel_error")
        assert isinstance(error, str) and error.strip(), case.kind
        assert "prelabel_s3_key" not in task
        assert not genv.prelabel_exists(job_id, task_id)
