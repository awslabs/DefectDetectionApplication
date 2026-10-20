"""
Property 6: the Device_Access_Document grants exactly the current
Device_Read_Buckets (portal-authorization-and-iam-scoping, design I6 "Test
strategy"; task 6.1).

For any set of use cases (ids, s3_bucket and additional_s3_buckets names in
and out of the dda-* / *-dda-* pattern, data buckets, optional
Results_Bucket_Overrides) granted, re-granted with changed buckets and revoked
in any order through `merge_document`, the document grants exactly the
out-of-pattern buckets of each current use case's `device_read_buckets` -
each with exactly the read actions on its two ARNs, each use case in exactly
one read statement - plus one results statement per out-of-pattern override;
nothing on '*', no ARN for an in-pattern bucket or a data bucket, and no
statement for a use case that has been revoked. `document_size` grows with
every further out-of-pattern bucket.

Pure: no handler, no client.

Validates: Requirements 8.2
"""
import json
import re

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import device_grants

PROPERTY_SETTINGS = settings(max_examples=100, deadline=None,
                             suppress_health_check=[HealthCheck.too_slow])

OUT_OF_PATTERN = st.sampled_from([
    "customer-media", "line-a-images", "acme-lake", "qa-shared", "factory-raw",
    "media-archive-2", "x" * 63, "plant.camera.feed"])
IN_PATTERN = st.sampled_from([
    "dda-media", "dda-models", "team-dda-images", "lab-dda-raw", "dda-inference-results-111111111111"])
# Training-data buckets live in their own namespace, so a data bucket is never
# also some use case's s3_bucket or allowed bucket.
DATA_BUCKETS = st.sampled_from([None, "train-data-a", "train-data-b", "dda-train-data"])
OVERRIDES = st.sampled_from([None, "", "dda-inference-results-111111111111", "fleet-results",
                             "line-results"])
ARN_RE = re.compile(r"arn:aws:s3:::[a-z0-9.-]+(/\*)?")


@st.composite
def bucket_fields(draw):
    return {
        "s3_bucket": draw(st.one_of(OUT_OF_PATTERN, IN_PATTERN)),
        "additional_s3_buckets": draw(st.lists(st.one_of(OUT_OF_PATTERN, IN_PATTERN), max_size=4)),
        "data_s3_bucket": draw(DATA_BUCKETS),
        "inference_uploader_s3_bucket": draw(OVERRIDES),
    }


@st.composite
def histories(draw):
    """Use cases and an order of operations over them: ('grant', i, fields)
    writes use case i with those bucket fields, ('revoke', i) removes it."""
    ids = draw(st.lists(st.uuids().map(str), min_size=1, max_size=5, unique=True))
    operations = draw(st.lists(st.one_of(
        st.tuples(st.just("grant"), st.integers(0, len(ids) - 1), bucket_fields()),
        st.tuples(st.just("revoke"), st.integers(0, len(ids) - 1))), min_size=1, max_size=12))
    return ids, operations


def expected_statements(usecase):
    """Independent restatement of what one use case must hold."""
    statements = {}
    buckets = sorted({name for name in [usecase["s3_bucket"], *usecase["additional_s3_buckets"]]
                      if not (name.startswith("dda-") or "-dda-" in name)})
    sid = "UseCase" + usecase["usecase_id"].replace("-", "")
    if buckets:
        statements[sid] = (["s3:GetObject", "s3:GetObjectVersion", "s3:GetBucketLocation"],
                           [arn for b in buckets for arn in (f"arn:aws:s3:::{b}", f"arn:aws:s3:::{b}/*")])
    override = usecase["inference_uploader_s3_bucket"]
    if override and not override.startswith("dda-inference-results-"):
        statements[sid + "Results"] = (
            ["s3:PutObject", "s3:PutObjectTagging", "s3:GetObject", "s3:GetBucketLocation"],
            [f"arn:aws:s3:::{override}", f"arn:aws:s3:::{override}/*"])
    return statements


def replay(ids, operations):
    document = None
    current = {}
    for operation in operations:
        usecase_id = ids[operation[1]]
        if operation[0] == "grant":
            usecase = {"usecase_id": usecase_id, **operation[2]}
            document, _ = device_grants.merge_document(
                document, usecase_id, device_grants.usecase_statements(usecase))
            current[usecase_id] = usecase
        else:
            document, _ = device_grants.merge_document(document, usecase_id, [])
            current.pop(usecase_id, None)
    return document, current


@PROPERTY_SETTINGS
@given(histories())
def test_property_document_grants_exactly_the_current_device_read_buckets(history):
    ids, operations = history
    document, current = replay(ids, operations)

    expected = {}
    for usecase in current.values():
        expected.update(expected_statements(usecase))
    statements = document["Statement"]
    sids = [statement["Sid"] for statement in statements]
    assert len(sids) == len(set(sids)), "a use case holds more than one statement of a kind"
    assert {s["Sid"]: (s["Action"], s["Resource"]) for s in statements} == expected

    revoked = set(ids) - set(current)
    in_pattern_or_data = set()
    for operation in operations:
        if operation[0] == "grant":
            fields = operation[2]
            in_pattern_or_data.update(
                name for name in [fields["s3_bucket"], *fields["additional_s3_buckets"]]
                if name.startswith("dda-") or "-dda-" in name)
            if fields["data_s3_bucket"]:
                in_pattern_or_data.add(fields["data_s3_bucket"])
    for statement in statements:
        assert statement["Effect"] == "Allow" and set(statement) == {"Sid", "Effect", "Action", "Resource"}
        assert re.match(device_grants.SID_RE, statement["Sid"])
        assert not any(sid.startswith("UseCase" + r.replace("-", "")) for r in revoked
                       for sid in [statement["Sid"]])
        for resource in statement["Resource"]:
            assert ARN_RE.fullmatch(resource), resource
            bucket = resource[len("arn:aws:s3:::"):].split("/", 1)[0]
            if not statement["Sid"].endswith("Results"):
                assert bucket not in in_pattern_or_data, resource


@PROPERTY_SETTINGS
@given(histories(), OUT_OF_PATTERN)
def test_property_document_size_grows_with_every_out_of_pattern_bucket(history, extra):
    ids, operations = history
    document, current = replay(ids, operations)
    for usecase in current.values():
        if extra in device_grants.device_read_buckets(usecase):
            continue
        grown = {**usecase, "additional_s3_buckets": [*usecase["additional_s3_buckets"], extra]}
        bigger, changed = device_grants.merge_document(
            document, usecase["usecase_id"], device_grants.usecase_statements(grown))
        assert changed
        assert device_grants.document_size(bigger) > device_grants.document_size(document)
        assert json.loads(json.dumps(bigger)) == bigger
