"""
Property tests for the use-case bucket configuration binding
(portal-usecase-bucket-config, design §1, §3, §14; tasks.md 6.4, P1–P5).

The oracle is hand-written from the record fields and never calls the
implementation: `oracle_set(record)` is the record's `s3_bucket`,
`data_s3_bucket` and `additional_s3_buckets` entries that are strings
matching the membership grammar `[A-Za-z0-9._-]{3,255}`, minus the two
portal-owned names read from the test environment (the artifacts bucket
and the component bucket). The record generator draws those two names into
every field with non-trivial probability, so the exclusion is exercised
rather than vacuous.

Properties (≥ 100 examples each, HYPOTHESIS_PROFILE=ci):

1. Soundness — a bucket outside the oracle set is refused by the shared
   helper with `assume_usecase_role`, `create_boto3_client` and
   `boto3.client` never called; a member is admitted.
2. Role preservation — a member is reached with exactly the pre-change
   role: the Data Account role iff the use case has a separate Data
   Account and the bucket is its `data_s3_bucket`, the cross-account role
   (the execution role for a root ARN) otherwise; every additional entry
   gets the cross-account role, never the Data Account role.
3. Exactness — an edit of an allowed name (prefix, suffix, embedding, case
   change, trusted-account substring) that is not itself a member is
   refused.
4. Parser agreement — the parser's bucket is the compared string and
   `s3://{bucket}/{key}` round-trips; a URI the parser rejects never
   reaches the helper.
5. Rejection precedes I/O — across the request-bucket handlers, a bucket
   outside the set answers 400 before any STS or S3 call.

Validates: Requirements 1.1–1.7, 2.10, 15.3
"""
import importlib
import json
import re
import sys
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

import shared_utils as su


def _bind_live_shared_utils():
    """Rebind `su` to the shared layer module the handler modules will import.

    An earlier aws_stack-based file in the same process pops `shared_utils`
    from sys.modules and re-imports it, so the module object bound at
    collection time may no longer be the one `from shared_utils import ...`
    resolves to; patching that stale object would leave the handlers on the
    real seams. Called first by every module-scoped fixture here.
    """
    global su
    su = importlib.import_module("shared_utils")
    assert hasattr(su, "assert_bucket_allowed"), (
        "a fake shared_utils is installed in sys.modules; this suite needs "
        "the real layer module")
    return su

ARTIFACTS = "test-portal-artifacts"   # conftest's PORTAL_ARTIFACTS_BUCKET
COMPONENT = "dda-component-us-east-1-portal"
PORTAL_OWNED = frozenset({ARTIFACTS, COMPONENT})
MEMBERSHIP = re.compile(r"[A-Za-z0-9._-]{3,255}")

CROSS_ROLE = "arn:aws:iam::acct-usecase:role/DDAPortalAccessRole"
ROOT_ARN = "arn:aws:iam::acct-usecase:root"
DATA_ROLE = "arn:aws:iam::acct-data:role/DDAPortalDataAccessRole"
USECASE_ID = "uc-prop"
USER_ID = "user-prop"


# ------------------------------------------------------------- the oracle

def oracle_set(record: Dict) -> set:
    """Hand-written Allowed_Bucket_Set: record fields ∩ membership grammar
    − portal-owned names. Never calls allowed_s3_buckets."""
    names = [record.get("s3_bucket"), record.get("data_s3_bucket"),
             *(record.get("additional_s3_buckets") or [])]
    return {name for name in names
            if isinstance(name, str) and MEMBERSHIP.fullmatch(name)} - PORTAL_OWNED


def expected_role(record: Dict, bucket: str):
    """Hand-written role choice: Data Account role iff a separate Data
    Account exists and the bucket is its data_s3_bucket."""
    if (record.get("data_account_id") and record["data_account_id"] != record.get("account_id")
            and bucket == record.get("data_s3_bucket")):
        return record.get("data_account_role_arn"), record.get("data_account_external_id")
    return record.get("cross_account_role_arn"), record.get("external_id")


# ----------------------------------------------------------- strategies

VALID_NAMES = ["uc-one-bucket", "uc-one-data", "uc-one-extra", "Legacy_Bucket",
               "a.b.c", "1.2.3.4", "x_y", "bucket-with-acct-usecase-in-name"]
INVALID_NAMES = ["", " ", "ab", "with space", "bad@host", "a/b", "host:9", "tab\tname",
                 "trail\n", "a" * 256]
FOREIGN_NAMES = ["someone-elses-bucket", "other-tenant-data", "uc-one-bucket-2"]

valid_name = st.one_of(st.sampled_from(VALID_NAMES), st.from_regex(MEMBERSHIP, fullmatch=True)
                       .filter(lambda s: len(s) <= 40 and s not in PORTAL_OWNED))
pool_name = st.one_of(
    st.sampled_from(VALID_NAMES + FOREIGN_NAMES + INVALID_NAMES),
    st.sampled_from(sorted(PORTAL_OWNED)),
    st.none(),
    st.integers(min_value=0, max_value=9),
    valid_name,
)


@st.composite
def records(draw):
    account = "acct-usecase"
    record = {
        "usecase_id": USECASE_ID,
        "account_id": account,
        "cross_account_role_arn": draw(st.sampled_from([CROSS_ROLE, ROOT_ARN])),
        "external_id": "ext-1",
    }
    if draw(st.booleans()):
        record["s3_bucket"] = draw(pool_name)
    if draw(st.booleans()):
        record["data_s3_bucket"] = draw(pool_name)
        record["data_account_id"] = draw(st.sampled_from([account, "acct-data"]))
        record["data_account_role_arn"] = DATA_ROLE
        record["data_account_external_id"] = "ext-data"
    additional = draw(st.one_of(st.none(), st.lists(pool_name, max_size=4)))
    if additional is not None:
        record["additional_s3_buckets"] = additional
    return record


candidate_name = st.one_of(pool_name, st.text(max_size=12))


# ------------------------------------------------------------- harness

class Recorder:
    def __init__(self, result=None):
        self.calls: List[Any] = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class FakeS3:
    def __init__(self):
        self.calls: List[Any] = []

    def __getattr__(self, name):
        def method(**kwargs):
            self.calls.append((name, kwargs))
            if name in ("head_object", "get_object", "download_file"):
                from botocore.exceptions import ClientError
                raise ClientError({"Error": {"Code": "404", "Message": "404"},
                                   "ResponseMetadata": {"HTTPStatusCode": 404}}, name)
            if name == "generate_presigned_url":
                return "https://signed.example/"
            return {"CommonPrefixes": [], "Contents": []}
        return method


@pytest.fixture(scope="module")
def rec():
    """Module-scoped recorders on the shared layer's STS / S3 seams (so the
    property bodies, which hypothesis forbids from using function-scoped
    fixtures, stay pure); each example clears them first."""
    _bind_live_shared_utils()
    mp = pytest.MonkeyPatch()
    mp.setenv("PORTAL_ARTIFACTS_BUCKET", ARTIFACTS)
    mp.setenv("COMPONENT_BUCKET", COMPONENT)
    mp.delenv("PORTAL_ACCOUNT_ID", raising=False)
    s3 = FakeS3()
    recorders = SimpleNamespace(
        assume=Recorder(result={"AccessKeyId": "k", "SecretAccessKey": "s", "SessionToken": "t"}),
        create_client=Recorder(result=s3),
        boto3_client=Recorder(result=s3),
        audit=Recorder(),
        s3=s3,
    )
    mp.setattr(su, "assume_usecase_role", recorders.assume)
    mp.setattr(su, "create_boto3_client", recorders.create_client)
    mp.setattr(su.boto3, "client", recorders.boto3_client)
    mp.setattr(su, "log_audit_event", recorders.audit)
    mp.setattr(su, "attribution_from", Recorder(result={}))

    def reset():
        for recorder in (recorders.assume, recorders.create_client,
                         recorders.boto3_client, recorders.audit):
            recorder.calls.clear()
        s3.calls.clear()

    recorders.reset = reset
    yield recorders
    mp.undo()


def assert_no_io(rec):
    assert rec.assume.calls == []
    assert rec.create_client.calls == []
    assert rec.boto3_client.calls == []
    assert rec.s3.calls == []


# ============================================================ Property 1

class TestProperty1Soundness:
    """**Feature: portal-usecase-bucket-config, Property 1: Soundness of the
    binding** — any bucket not in the hand-written oracle set is refused
    with empty STS/S3 recorders; the portal-owned names are refused even
    when the record names them. Validates: Requirements 1.2, 1.3, 1.4, 2.10"""

    @settings(max_examples=150, deadline=None)
    @given(record=records(), candidate=candidate_name)
    def test_outside_the_oracle_set_is_refused_before_any_io(self, rec, record, candidate):
        rec.reset()
        allowed = oracle_set(record)
        if candidate in allowed:
            client = su.get_s3_client_for_bucket(record, candidate, "p1")
            assert client is rec.s3
            assert len(rec.assume.calls) == 1
            assert rec.audit.calls == []
            return
        with pytest.raises(su.BucketConfigError) as excinfo:
            su.get_s3_client_for_bucket(record, candidate, "p1")
        grammar_ok = isinstance(candidate, str) and MEMBERSHIP.fullmatch(candidate)
        assert excinfo.value.error_code == ("bucket_not_allowed" if grammar_ok
                                            else "invalid_bucket_name")
        if grammar_ok and candidate in PORTAL_OWNED:
            assert excinfo.value.remedy == su.REMEDY_PORTAL_BUCKET
        assert_no_io(rec)
        # One s3_bucket_refused audit row per refused name; none for a
        # grammar failure (nothing was decided).
        assert len(rec.audit.calls) == (1 if grammar_ok else 0)

    @settings(max_examples=100, deadline=None)
    @given(record=records())
    def test_allowed_set_equals_the_oracle(self, rec, record):
        assert set(su.allowed_s3_buckets(record)) == oracle_set(record)
        assert not (set(su.allowed_s3_buckets(record)) & PORTAL_OWNED)


# ============================================================ Property 2

class TestProperty2RolePreservation:
    """**Feature: portal-usecase-bucket-config, Property 2: Role
    preservation** — every member of the set is reached with the
    pre-change role choice, and every additional entry with the
    cross-account role, never the Data Account role.
    Validates: Requirements 1.5, 1.7"""

    @settings(max_examples=150, deadline=None)
    @given(record=records(), data=st.data())
    def test_member_gets_the_pre_change_role(self, rec, record, data):
        allowed = sorted(oracle_set(record))
        assume(allowed)
        bucket = data.draw(st.sampled_from(allowed))
        rec.reset()

        su.get_s3_client_for_bucket(record, bucket, "p2-session")

        (args, _), = rec.assume.calls
        assert args == (*expected_role(record, bucket), "p2-session")
        assert su._select_s3_role_for_bucket(record, bucket) == expected_role(record, bucket)
        (client_args, _), = rec.create_client.calls
        assert client_args == ("s3", rec.assume.result)

    @settings(max_examples=100, deadline=None)
    @given(record=records(), data=st.data())
    def test_additional_entries_never_get_the_data_account_role(self, rec, record, data):
        additional = [name for name in (record.get("additional_s3_buckets") or [])
                      if name in oracle_set(record) and name != record.get("data_s3_bucket")]
        assume(additional)
        bucket = data.draw(st.sampled_from(additional))
        rec.reset()

        su.get_s3_client_for_bucket(record, bucket, "p2-additional")

        (args, _), = rec.assume.calls
        assert args[0] == record["cross_account_role_arn"]
        assert args[0] != DATA_ROLE


# ============================================================ Property 3

def mutations(name: str):
    swapped = name.swapcase()
    return [f"x{name}", f"{name}x", f"{name}-2", f"pre-{name}-post", name[:-1], f"{name}.",
            swapped if swapped != name else f"{name}-S", "acct-usecase", f"{name}acct-usecase",
            name.upper() if name.upper() != name else f"{name}U"]


class TestProperty3Exactness:
    """**Feature: portal-usecase-bucket-config, Property 3: Exactness** —
    edits of an allowed name (prefix, suffix, embedding, case change,
    trusted-account substring) that are not themselves members are
    refused. Validates: Requirements 1.4"""

    @settings(max_examples=150, deadline=None)
    @given(record=records(), data=st.data())
    def test_edited_names_are_refused(self, rec, record, data):
        allowed = oracle_set(record)
        assume(allowed)
        name = data.draw(st.sampled_from(sorted(allowed)))
        mutated = data.draw(st.sampled_from(mutations(name)))
        assume(mutated not in allowed)
        rec.reset()

        with pytest.raises(su.BucketConfigError):
            su.get_s3_client_for_bucket(record, mutated, "p3")
        assert_no_io(rec)


# ============================================================ Property 4

key_text = st.text(min_size=1, max_size=24).filter(lambda k: not k.startswith("/") or len(k) > 1)
malformed_uris = st.one_of(
    st.text(max_size=24).filter(lambda s: not s.startswith("s3://")),
    st.just("s3://"),
    st.builds(lambda b: f"s3://{b}", valid_name),              # no key
    st.builds(lambda b: f"s3://{b}/", valid_name),             # empty key
    st.builds(lambda b, k: f"S3://{b}/{k}", valid_name, key_text),
    st.builds(lambda b, k: f"s3://{b}/{k}", st.sampled_from(INVALID_NAMES), key_text),
    st.builds(lambda b, k: f"s3://{b}@evil/{k}", valid_name, key_text),
    st.builds(lambda b, k: f"s3://{b}:9/{k}", valid_name, key_text),
)


class TestProperty4ParserAgreement:
    """**Feature: portal-usecase-bucket-config, Property 4: Parser
    agreement** — the parser's bucket is the compared string and
    round-trips; a URI the parser rejects never reaches the helper.
    Validates: Requirements 1.1, 1.4"""

    @settings(max_examples=150, deadline=None)
    @given(record=records(), bucket=valid_name, key=key_text)
    def test_parsed_bucket_is_the_compared_string(self, rec, record, bucket, key):
        uri = f"s3://{bucket}/{key}"
        parsed_bucket, parsed_key = su.parse_s3_uri(uri)
        assert (parsed_bucket, parsed_key) == (bucket, key)
        assert f"s3://{parsed_bucket}/{parsed_key}" == uri
        rec.reset()
        if parsed_bucket in oracle_set(record):
            su.get_s3_client_for_bucket(record, parsed_bucket, "p4")
            assert len(rec.assume.calls) == 1
        else:
            with pytest.raises(su.BucketNotAllowed):
                su.get_s3_client_for_bucket(record, parsed_bucket, "p4")
            assert_no_io(rec)

    @settings(max_examples=150, deadline=None)
    @given(uri=malformed_uris)
    def test_rejected_uris_never_reach_the_helper(self, rec, uri):
        rec.reset()
        with pytest.raises(su.InvalidS3Uri):
            su.parse_s3_uri(uri)
        # The consolidated artifact validator parses first: a rejected URI
        # costs no credential step and no download.
        mi = importlib.import_module("model_import")
        with pytest.raises(su.InvalidS3Uri):
            mi.validate_model_artifact(uri, {"usecase_id": USECASE_ID, "s3_bucket": "any-bucket"},
                                       purpose="p4")
        assert_no_io(rec)


# ============================================================ Property 5

@pytest.fixture(scope="module")
def handlers(rec):
    """Fresh handler modules with RBAC admitting, the use case lookup
    reading the example's record, and recorders on their own credential
    paths."""
    _bind_live_shared_utils()
    mp = pytest.MonkeyPatch()
    modules = {}
    for name in ("training", "model_import", "model_converter", "pre_labeled_datasets",
                 "data_management"):
        sys.modules.pop(name, None)
        modules[name] = importlib.import_module(name)
    holder = {"record": {}}
    for module in modules.values():
        mp.setattr(module, "check_user_access", lambda *a, **k: True)
        mp.setattr(module, "log_audit_event", Recorder())
    lookup = lambda _id: dict(holder["record"])  # noqa: E731
    mp.setattr(modules["training"], "get_usecase", lookup)
    mp.setattr(modules["pre_labeled_datasets"], "get_usecase", lookup)
    mp.setattr(modules["data_management"], "get_usecase", lookup)
    mp.setattr(modules["model_import"], "get_usecase_details", lookup)
    mp.setattr(modules["model_converter"], "get_usecase_details", lookup)
    local = SimpleNamespace(
        usecase_client=Recorder(result=rec.s3),
        converter_assume=Recorder(result={"is_default_credentials": True}),
        converter_client=Recorder(result=rec.s3),
        data_credentials=Recorder(result=None),
        dynamo_items=[],
    )
    mp.setattr(modules["model_import"], "get_usecase_client", local.usecase_client)
    mp.setattr(modules["model_converter"], "assume_usecase_role", local.converter_assume)
    mp.setattr(modules["model_converter"], "make_usecase_s3_client", local.converter_client)
    mp.setattr(modules["data_management"], "get_data_account_credentials",
               local.data_credentials)
    mp.setattr(modules["data_management"], "ensure_bucket_cors", Recorder())
    table = SimpleNamespace(put_item=lambda Item: local.dynamo_items.append(Item),
                            get_item=lambda Key: {}, query=lambda **kw: {"Items": []})
    for name in ("training", "model_import", "pre_labeled_datasets"):
        mp.setattr(modules[name], "dynamodb", SimpleNamespace(Table=lambda _n: table))
    yield SimpleNamespace(m=SimpleNamespace(**modules), holder=holder, local=local)
    mp.undo()


def _event(body=None, query=None, path_params=None):
    return {"httpMethod": "POST", "path": "/p5", "resource": "/p5",
            "body": json.dumps(body) if body is not None else None,
            "queryStringParameters": query, "pathParameters": path_params,
            "requestContext": {"authorizer": {"claims": {
                "sub": USER_ID, "email": "user-prop@example.com",
                "cognito:username": USER_ID, "custom:role": "DataScientist"}}}}


def _user():
    return {"user_id": USER_ID, "email": "user-prop@example.com", "username": USER_ID,
            "role": "DataScientist"}


SINKS = {
    "validate_model": lambda m, b: m.model_import.validate_model(_event(
        {"usecase_id": USECASE_ID, "model_s3_uri": f"s3://{b}/m.tar.gz"}), None),
    "import_model": lambda m, b: m.model_import.import_model(_event(
        {"usecase_id": USECASE_ID, "model_name": "m", "model_version": "1",
         "model_s3_uri": f"s3://{b}/m.tar.gz"}), None),
    "register_vllm_model": lambda m, b: m.model_import.register_vllm_model(_event(
        {"usecase_id": USECASE_ID, "model_name": "m", "model_version": "1",
         "s3_model_artifact": f"s3://{b}/m.tar.gz"}), None),
    "create_training_job": lambda m, b: m.training.create_training_job(_event(
        {"usecase_id": USECASE_ID, "model_name": "m", "model_version": "1",
         "model_type": "classification", "model_source": "custom",
         "dataset_manifest_s3": f"s3://{b}/train.manifest"}), None),
    "transform_manifest": lambda m, b: m.training.transform_manifest(_event(
        {"usecase_id": USECASE_ID, "source_manifest_uri": f"s3://{b}/train.manifest"}), None),
    "convert_model": lambda m, b: m.model_converter.convert_model(_event(
        {"usecase_id": USECASE_ID, "model_s3_uri": f"s3://{b}/model.pt", "model_name": "m",
         "model_type": "classification", "image_width": 224, "image_height": 224}), None),
    "inspect_model_endpoint": lambda m, b: m.model_converter.inspect_model_endpoint(_event(
        {"usecase_id": USECASE_ID, "model_s3_uri": f"s3://{b}/model.pt"}), None),
    "pld.validate_manifest": lambda m, b: m.pre_labeled_datasets.validate_manifest(_event(
        {"usecase_id": USECASE_ID, "manifest_s3_uri": f"s3://{b}/a.manifest"}), _user()),
    "pld.create_dataset": lambda m, b: m.pre_labeled_datasets.create_dataset(_event(
        {"usecase_id": USECASE_ID, "name": "d", "manifest_s3_uri": f"s3://{b}/a.manifest"}),
        _user()),
    "list_folders": lambda m, b: m.data_management.list_folders(_event(
        query={"bucket": b}, path_params={"id": USECASE_ID})),
    "create_folder": lambda m, b: m.data_management.create_folder(_event(
        {"bucket": b, "folder_path": "raw/"}, path_params={"id": USECASE_ID})),
    "get_upload_url": lambda m, b: m.data_management.get_upload_url(_event(
        {"bucket": b, "key": "raw/a.jpg"}, path_params={"id": USECASE_ID})),
    "get_batch_upload_urls": lambda m, b: m.data_management.get_batch_upload_urls(_event(
        {"bucket": b, "files": [{"filename": "a.jpg"}]}, path_params={"id": USECASE_ID})),
}

# Buckets that make a well-formed request (the handlers' own shape checks
# accept them) but are outside any generated record's set unless drawn in.
refusal_candidates = st.one_of(st.sampled_from(FOREIGN_NAMES + sorted(PORTAL_OWNED)), valid_name)


class TestProperty5RejectionPrecedesIO:
    """**Feature: portal-usecase-bucket-config, Property 5: Rejection
    precedes I/O** — for every request-bucket handler and any bucket
    outside the record's set, the answer is 400 with `bucket_not_allowed`
    before any STS or S3 call. Validates: Requirements 2.1–2.8, 2.10, 15.1"""

    @settings(max_examples=150, deadline=None)
    @given(record=records(), sink=st.sampled_from(sorted(SINKS)), bucket=refusal_candidates)
    def test_foreign_bucket_answers_400_before_io(self, rec, handlers, record, sink, bucket):
        assume(bucket not in oracle_set(record))
        rec.reset()
        local = handlers.local
        for recorder in (local.usecase_client, local.converter_assume,
                         local.converter_client, local.data_credentials):
            recorder.calls.clear()
        local.dynamo_items.clear()
        handlers.holder["record"] = record

        response = SINKS[sink](handlers.m, bucket)

        assert response["statusCode"] == 400, response["body"]
        body = json.loads(response["body"])
        assert body["error_code"] == "bucket_not_allowed"
        assert body["bucket"] == bucket
        assert body["remedy"] == (su.REMEDY_PORTAL_BUCKET if bucket in PORTAL_OWNED
                                  else su.REMEDY_NOT_ALLOWED)
        assert_no_io(rec)
        assert local.usecase_client.calls == []
        assert local.converter_assume.calls == [] and local.converter_client.calls == []
        assert local.data_credentials.calls == []
        assert local.dynamo_items == []
