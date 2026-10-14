"""
Unit tests for the use-case bucket configuration primitives of the shared
layer (portal-usecase-bucket-config, design §1, §2.4; tasks.md 1.4).

Covers, without AWS: the URI parser and the two grammars, the remedy and
error envelope, `portal_owned_buckets` under its environment permutations,
`allowed_s3_buckets` composition (portal-owned names dropped by rule),
`assert_bucket_allowed` / `get_s3_client_for_bucket` refusal with empty
STS/S3 recorders and role preservation for members, the `s3_bucket_refused`
audit row, `probe_bucket_eligibility`'s outcome table (retry only when
asked, one region redirect), `check_bucket_attachable`'s ordered decision
with stubbed Scan / get_item / probe, and the writer guard over the handler
modules.

Validates: Requirements 1.1–1.7, 5.1–5.3, 10.1–10.3, 10.5, 10.10, 10.11,
15.1, 15.9
"""
import ast
import os
import sys
from typing import Any, Dict, List

import pytest
from botocore.exceptions import ClientError

import shared_utils as su

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_FUNCTIONS_DIR = os.path.join(_BACKEND, "functions")
_SHARED_LAYER = os.path.join(_BACKEND, "layers", "shared", "python")
_FIXTURE_MODULE = os.path.join(_HERE, "fixtures", "bucket_attribute_writers.py")

ARTIFACTS = "test-portal-artifacts"   # conftest's PORTAL_ARTIFACTS_BUCKET
COMPONENT = "dda-component-us-east-1-portal"
FOREIGN = "someone-elses-bucket"


class Recorder:
    """Callable that records every call and returns a fixed value."""

    def __init__(self, result=None, raises=None):
        self.calls: List[Any] = []
        self.result = result
        self.raises = raises

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.result


def client_error(code: str, status: int, operation: str = "GetBucketTagging",
                 headers: Dict[str, str] = None) -> ClientError:
    response = {
        "Error": {"Code": code, "Message": code},
        "ResponseMetadata": {"HTTPStatusCode": status, "HTTPHeaders": headers or {}},
    }
    return ClientError(response, operation)


def tagging(**tags) -> Dict:
    """A GetBucketTagging response; in the keys '__' stands for ':' and '_'
    for '-' (dda_portal__created_by -> dda-portal:created-by)."""
    return {"TagSet": [{"Key": key.replace("__", ":").replace("_", "-"), "Value": value}
                       for key, value in tags.items()]}


@pytest.fixture
def portal_env(monkeypatch):
    """The two portal-owned names: artifacts from conftest, component explicit."""
    monkeypatch.setenv("PORTAL_ARTIFACTS_BUCKET", ARTIFACTS)
    monkeypatch.setenv("COMPONENT_BUCKET", COMPONENT)
    monkeypatch.delenv("PORTAL_ACCOUNT_ID", raising=False)
    return frozenset({ARTIFACTS, COMPONENT})


@pytest.fixture
def quiet_audit(monkeypatch):
    """Recorder on log_audit_event so no refusal reaches DynamoDB."""
    recorder = Recorder()
    monkeypatch.setattr(su, "log_audit_event", recorder)
    return recorder


@pytest.fixture
def no_aws(monkeypatch):
    """Recorders on every path to STS/S3: all must stay empty on a refusal."""
    recorders = {
        "assume_usecase_role": Recorder(result={"AccessKeyId": "k", "SecretAccessKey": "s",
                                                "SessionToken": "t"}),
        "create_boto3_client": Recorder(result=object()),
        "boto3.client": Recorder(result=object()),
    }
    monkeypatch.setattr(su, "assume_usecase_role", recorders["assume_usecase_role"])
    monkeypatch.setattr(su, "create_boto3_client", recorders["create_boto3_client"])
    monkeypatch.setattr(su.boto3, "client", recorders["boto3.client"])
    return recorders


def usecase_record(**overrides) -> Dict:
    record = {
        "usecase_id": "uc-1",
        "account_id": "acct-usecase",
        "s3_bucket": "uc-one-bucket",
        "cross_account_role_arn": "arn:aws:iam::acct-usecase:role/DDAPortalAccessRole",
        "external_id": "ext-1",
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# §1.1 grammars and parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("uri, require_key, expected", [
    ("s3://my.bucket-1/a/b.tar.gz", True, ("my.bucket-1", "a/b.tar.gz")),
    ("s3://bucket", False, ("bucket", "")),
    ("s3://Legacy_Bucket/key", True, ("Legacy_Bucket", "key")),
    ("s3://a..b/key", True, ("a..b", "key")),            # membership grammar
    ("s3://1.2.3.4/key", True, ("1.2.3.4", "key")),      # membership grammar
    ("s3://bucket/models/x/", True, ("bucket", "models/x/")),   # trailing slash: non-empty key
    ("s3://bucket//key", True, ("bucket", "/key")),      # split at the FIRST slash, key as-is
    ("s3://bucket/Key With Space.tar.gz", True, ("bucket", "Key With Space.tar.gz")),
])
def test_parse_s3_uri_accepts(uri, require_key, expected):
    assert su.parse_s3_uri(uri, require_key=require_key) == expected


@pytest.mark.parametrize("uri, require_key", [
    ("s3://", True),
    ("s3://", False),
    ("s3://bucket", True),                 # key required
    ("s3://bucket/", True),                # empty key
    ("S3://bucket/key", True),             # literal lowercase scheme only
    ("s3:/bucket/key", True),
    ("http://bucket/key", True),
    ("bucket/key", True),
    ("s3://bucket@other/key", True),       # urlparse netloc would be bucket@other
    ("s3://bucket:443/key", True),
    ("s3://../key", True),
    ("s3://ab/key", True),                 # shorter than the grammar's minimum
    (" s3://bucket/key", True),            # never stripped
    ("s3://buck et/key", True),            # whitespace
    ("s3://bucket\n/key", True),           # control character
    ("s3://bucket%2Fother/key", True),     # never percent-decoded; '%' is outside the class
    ("s3://" + "b" * 2040 + "/key", True), # 2049 characters
])
def test_parse_s3_uri_rejects(uri, require_key):
    with pytest.raises(su.InvalidS3Uri) as info:
        su.parse_s3_uri(uri, require_key=require_key)
    assert info.value.error_code == "invalid_s3_uri"


def test_parse_s3_uri_length_bound_is_exact():
    exactly = "s3://bucket/" + "k" * 2036
    assert len(exactly) == 2048
    assert su.parse_s3_uri(exactly) == ("bucket", "k" * 2036)
    with pytest.raises(su.InvalidS3Uri):
        su.parse_s3_uri(exactly + "k")


@pytest.mark.parametrize("value", [None, 42, ["s3://bucket/key"], {"uri": "s3://bucket/key"}, b"s3://bucket/key"])
def test_parse_s3_uri_rejects_non_strings(value):
    with pytest.raises(su.InvalidS3Uri):
        su.parse_s3_uri(value)


@pytest.mark.parametrize("name, membership, strict", [
    ("my-bucket", True, True),
    ("my.bucket-1", True, True),
    ("Legacy_Bucket", True, False),
    ("a..b", True, False),
    ("1.2.3.4", True, False),
    ("abc", True, True),
    ("ab", False, False),
    ("a" * 63, True, True),
    ("a" * 64, True, False),
    ("a" * 255, True, False),
    ("a" * 256, False, False),
    ("-abc", True, False),
    ("abc-", True, False),
    ("abc\n", False, False),
    ("ab c", False, False),
    ("bucket/key", False, False),
    ("bucket:443", False, False),
    ("bucket@other", False, False),
    ("", False, False),
])
def test_grammars(name, membership, strict):
    assert su.is_membership_name(name) is membership
    assert su.is_valid_bucket_name(name) is strict


@pytest.mark.parametrize("value", [None, 42, ["abc"], {"name": "abc"}])
def test_grammars_reject_non_strings(value):
    assert su.is_membership_name(value) is False
    assert su.is_valid_bucket_name(value) is False


# ---------------------------------------------------------------------------
# §1.2 errors, remedies, envelope
# ---------------------------------------------------------------------------

def test_error_codes_per_class():
    assert su.InvalidS3Uri("m").error_code == "invalid_s3_uri"
    assert su.InvalidBucketName("m").error_code == "invalid_bucket_name"
    assert su.BucketNotAllowed("m").error_code == "bucket_not_allowed"
    assert su.BucketNotEligible("m").error_code == "bucket_not_eligible"
    for cls in (su.InvalidS3Uri, su.InvalidBucketName, su.BucketNotAllowed, su.BucketNotEligible):
        assert issubclass(cls, su.BucketConfigError) and issubclass(cls, ValueError)


def test_to_body_drops_none_values_and_non_string_bucket():
    exc = su.BucketNotEligible("refused", bucket="b-1", usecase_id="uc-1", purpose="p",
                               reason="no_tags", remedy=su.REMEDY_TAG)
    assert exc.to_body() == {
        "error": "refused", "error_code": "bucket_not_eligible", "bucket": "b-1",
        "usecase_id": "uc-1", "purpose": "p", "reason": "no_tags", "remedy": su.REMEDY_TAG,
    }
    assert su.InvalidBucketName("bad", bucket=42).to_body() == {
        "error": "bad", "error_code": "invalid_bucket_name"}
    assert "bucket" not in su.InvalidBucketName("bad", bucket=["x"]).to_body()


def test_bucket_config_error_response_envelope():
    exc = su.BucketNotAllowed("refused", bucket="b-1", usecase_id="uc-1", purpose="p",
                              remedy=su.REMEDY_NOT_ALLOWED)
    response = su.bucket_config_error_response(exc)
    assert response["statusCode"] == 400
    import json
    assert json.loads(response["body"]) == exc.to_body()
    assert su.bucket_config_error_response(exc, status=403)["statusCode"] == 403


def test_ten_remedy_constants_use_the_s3control_form():
    remedies = {name: getattr(su, name) for name in dir(su) if name.startswith("REMEDY_")}
    remedies.pop("REMEDY_FOR_REASON")
    assert set(remedies) == {
        "REMEDY_NOT_ALLOWED", "REMEDY_PORTAL_BUCKET", "REMEDY_TAG", "REMEDY_TAGGING_UNREADABLE",
        "REMEDY_NOT_FOUND", "REMEDY_CREDENTIALS", "REMEDY_ACCOUNT", "REMEDY_OTHER_USECASE",
        "REMEDY_STALE_CLAIM", "REMEDY_TOO_MANY"}
    for text in remedies.values():
        assert isinstance(text, str) and text
        assert "s3api tag-resource" not in text
    for name in ("REMEDY_TAG", "REMEDY_TAGGING_UNREADABLE"):
        assert "aws s3control tag-resource --account-id" in remedies[name]
        assert "--resource-arn arn:aws:s3:::<bucket>" in remedies[name]
        assert "Key=dda-portal:managed,Value=true" in remedies[name]
    assert "aws s3control untag-resource --account-id" in su.REMEDY_STALE_CLAIM
    assert "--tag-keys dda-portal:usecase-id" in su.REMEDY_STALE_CLAIM
    # tagging_unreadable names the untagged cause first and the allowlist cause last
    unreadable = su.REMEDY_TAGGING_UNREADABLE
    assert unreadable.index("if it is not tagged") < unreadable.index("enable ABAC") \
        < unreadable.index("data-bucket allowlist")
    assert "configured on, or tagged for, another use case" in su.REMEDY_OTHER_USECASE
    assert "no longer exists" in su.REMEDY_STALE_CLAIM
    assert "at most 100 additional buckets" in su.REMEDY_TOO_MANY


def test_remedy_for_reason_table():
    assert su.REMEDY_FOR_REASON == {
        "tag_missing": su.REMEDY_TAG, "no_tags": su.REMEDY_TAG,
        "tagging_unreadable": su.REMEDY_TAGGING_UNREADABLE,
        "bucket_not_found": su.REMEDY_NOT_FOUND,
        "credentials_unavailable": su.REMEDY_CREDENTIALS,
        "portal_bucket": su.REMEDY_PORTAL_BUCKET,
        "account_mismatch": su.REMEDY_ACCOUNT,
        "too_many_buckets": su.REMEDY_TOO_MANY,
    }
    assert "bucket_configured_on_other_usecase" not in su.REMEDY_FOR_REASON


# ---------------------------------------------------------------------------
# §1.3 portal_owned_buckets and allowed_s3_buckets
# ---------------------------------------------------------------------------

def test_portal_owned_buckets_both_set(monkeypatch):
    monkeypatch.setenv("PORTAL_ARTIFACTS_BUCKET", "arts")
    monkeypatch.setenv("COMPONENT_BUCKET", "comp-explicit")
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", "acct-portal")
    assert su.portal_owned_buckets() == frozenset({"arts", "comp-explicit"})


def test_portal_owned_buckets_prefix_derivation(monkeypatch):
    monkeypatch.setenv("PORTAL_ARTIFACTS_BUCKET", "arts")
    monkeypatch.delenv("COMPONENT_BUCKET", raising=False)
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", "acct-portal")
    monkeypatch.setenv("COMPONENT_BUCKET_PREFIX", "custom-prefix")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    assert su.portal_owned_buckets() == frozenset({"arts", "custom-prefix-eu-west-1-acct-portal"})
    monkeypatch.delenv("COMPONENT_BUCKET_PREFIX")
    monkeypatch.delenv("AWS_REGION")
    assert su.portal_owned_buckets() == frozenset({"arts", "dda-component-us-east-1-acct-portal"})


def test_portal_owned_buckets_without_account_id_is_artifacts_only(monkeypatch):
    monkeypatch.setenv("PORTAL_ARTIFACTS_BUCKET", "arts")
    monkeypatch.delenv("COMPONENT_BUCKET", raising=False)
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", "")
    assert su.portal_owned_buckets() == frozenset({"arts"})
    monkeypatch.delenv("PORTAL_ACCOUNT_ID")
    assert su.portal_owned_buckets() == frozenset({"arts"})


def test_portal_owned_buckets_nothing_set(monkeypatch):
    for key in ("PORTAL_ARTIFACTS_BUCKET", "COMPONENT_BUCKET", "PORTAL_ACCOUNT_ID"):
        monkeypatch.delenv(key, raising=False)
    assert su.portal_owned_buckets() == frozenset()


@pytest.mark.parametrize("record, expected", [
    ({"s3_bucket": "cfg-bucket"}, {"cfg-bucket"}),
    ({"s3_bucket": "cfg-bucket", "data_s3_bucket": ""}, {"cfg-bucket"}),
    ({"s3_bucket": "cfg-bucket", "data_s3_bucket": None}, {"cfg-bucket"}),
    ({"s3_bucket": "cfg-bucket", "data_s3_bucket": "   "}, {"cfg-bucket"}),
    ({"s3_bucket": "cfg-bucket", "additional_s3_buckets": []}, {"cfg-bucket"}),
    ({"s3_bucket": "cfg-bucket", "additional_s3_buckets": None}, {"cfg-bucket"}),
    ({"s3_bucket": "cfg-bucket", "data_s3_bucket": "data-bucket",
      "additional_s3_buckets": ["extra-1", "extra-2"]},
     {"cfg-bucket", "data-bucket", "extra-1", "extra-2"}),
    ({"s3_bucket": "Legacy_Bucket"}, {"Legacy_Bucket"}),
    ({"s3_bucket": "cfg-bucket", "additional_s3_buckets": ["cfg-bucket"]}, {"cfg-bucket"}),
    ({}, set()),
])
def test_allowed_s3_buckets_composition(portal_env, record, expected):
    assert su.allowed_s3_buckets(dict(record, usecase_id="uc-1")) == frozenset(expected)


def test_allowed_s3_buckets_drops_grammar_failures_with_warning(portal_env, caplog):
    record = usecase_record(additional_s3_buckets=["good-bucket", "bad/name", "ab", 42, None, "x y"])
    with caplog.at_level("WARNING"):
        assert su.allowed_s3_buckets(record) == frozenset({"uc-one-bucket", "good-bucket"})
    warnings = [r for r in caplog.records if "invalid name" in r.getMessage()]
    assert len(warnings) == 4
    assert all("uc-1" in r.getMessage() for r in warnings)
    assert not any("bad/name" in r.getMessage() for r in warnings)


def test_allowed_s3_buckets_drops_portal_owned_names_by_rule(portal_env, caplog):
    record = {"usecase_id": "uc-1", "s3_bucket": ARTIFACTS, "additional_s3_buckets": [COMPONENT]}
    with caplog.at_level("WARNING"):
        assert su.allowed_s3_buckets(record) == frozenset()
    warnings = [r.getMessage() for r in caplog.records if "portal-owned" in r.getMessage()]
    assert len(warnings) == 2 and all("uc-1" in w for w in warnings)


def test_allowed_s3_buckets_keeps_valid_members_next_to_portal_owned(portal_env):
    record = {"usecase_id": "uc-1", "s3_bucket": "real-bucket", "data_s3_bucket": ARTIFACTS,
              "additional_s3_buckets": [COMPONENT, "extra-1"]}
    assert su.allowed_s3_buckets(record) == frozenset({"real-bucket", "extra-1"})


def test_allowed_s3_buckets_is_exact_name_membership(portal_env):
    record = usecase_record(s3_bucket="my-bucket")
    allowed = su.allowed_s3_buckets(record)
    assert "my-bucket" in allowed
    for near in ("my-bucket-2", "my-bucke", "MY-BUCKET", " my-bucket", "my-bucket/"):
        assert near not in allowed


# ---------------------------------------------------------------------------
# §1.4 assert_bucket_allowed and the audit row
# ---------------------------------------------------------------------------

def test_assert_bucket_allowed_member_returns_without_audit(portal_env, quiet_audit):
    assert su.assert_bucket_allowed(usecase_record(), "uc-one-bucket", "training-manifest") is None
    assert quiet_audit.calls == []


@pytest.mark.parametrize("bucket", [None, 42, ["uc-one-bucket"], {"b": 1}, "ab", "bad/name", "x y", ""])
def test_assert_bucket_allowed_grammar_failure_is_invalid_bucket_name_without_audit(
        portal_env, quiet_audit, bucket):
    with pytest.raises(su.InvalidBucketName) as info:
        su.assert_bucket_allowed(usecase_record(), bucket, "p")
    assert info.value.error_code == "invalid_bucket_name"
    assert info.value.purpose == "p" and info.value.usecase_id == "uc-1"
    assert quiet_audit.calls == []


def test_assert_bucket_allowed_refuses_foreign_bucket_with_not_allowed_remedy(
        portal_env, quiet_audit, caplog):
    with caplog.at_level("WARNING"):
        with pytest.raises(su.BucketNotAllowed) as info:
            su.assert_bucket_allowed(usecase_record(), FOREIGN, "training-manifest")
    exc = info.value
    assert exc.error_code == "bucket_not_allowed"
    assert exc.bucket == FOREIGN and exc.usecase_id == "uc-1" and exc.purpose == "training-manifest"
    assert exc.remedy == su.REMEDY_NOT_ALLOWED
    messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(f"usecase=uc-1 bucket={FOREIGN} purpose=training-manifest" in m for m in messages)
    assert len(quiet_audit.calls) == 1


@pytest.mark.parametrize("owned", [ARTIFACTS, COMPONENT])
def test_assert_bucket_allowed_refuses_portal_owned_even_when_the_record_names_it(
        portal_env, quiet_audit, owned):
    for record in (usecase_record(),
                   usecase_record(s3_bucket=owned),
                   usecase_record(data_s3_bucket=owned),
                   usecase_record(additional_s3_buckets=[owned])):
        with pytest.raises(su.BucketNotAllowed) as info:
            su.assert_bucket_allowed(record, owned, "p")
        assert info.value.remedy == su.REMEDY_PORTAL_BUCKET
        assert info.value.error_code == "bucket_not_allowed"


def test_assert_bucket_allowed_has_no_opt_in_parameter():
    import inspect
    params = inspect.signature(su.assert_bucket_allowed).parameters
    assert set(params) == {"usecase", "bucket", "purpose", "actor", "event"}
    assert not any("allow" in name for name in params)
    source = inspect.getsource(su)
    assert "allow_portal" not in source


def test_refusal_audit_row_shape_without_actor(portal_env, quiet_audit):
    with pytest.raises(su.BucketNotAllowed):
        su.assert_bucket_allowed(usecase_record(), FOREIGN, "training-manifest")
    (args, kwargs), = quiet_audit.calls
    assert args == ()
    assert kwargs == {
        "user_id": "system", "action": "s3_bucket_refused", "resource_type": "usecase",
        "resource_id": "uc-1", "result": "denied",
        "details": {"bucket": FOREIGN, "purpose": "training-manifest"},
        "identity": None,
    }


def test_refusal_audit_row_shape_with_actor_and_event(portal_env, quiet_audit, monkeypatch):
    attribution = Recorder(result={"username": "u", "email": "e", "source_ip": "1.2.3.4",
                                   "user_agent": "ua", "identity_source": "registry"})
    monkeypatch.setattr(su, "attribution_from", attribution)
    actor = {"user_id": "user-7", "username": "u"}
    event = {"requestContext": {"identity": {"sourceIp": "1.2.3.4"}}}
    with pytest.raises(su.BucketNotAllowed):
        su.assert_bucket_allowed(usecase_record(), FOREIGN, "model-import", actor=actor, event=event)
    (_, kwargs), = quiet_audit.calls
    assert kwargs["user_id"] == "user-7"
    assert kwargs["details"] == {"bucket": FOREIGN, "purpose": "model-import"}
    assert kwargs["identity"] == attribution.result
    assert attribution.calls == [((event, actor, "uc-1"), {})]


def test_refusal_audit_row_unknown_usecase_id(portal_env, quiet_audit):
    with pytest.raises(su.BucketNotAllowed):
        su.assert_bucket_allowed({"s3_bucket": "cfg-bucket"}, FOREIGN, "p")
    (_, kwargs), = quiet_audit.calls
    assert kwargs["resource_id"] == "unknown"


def test_audit_failure_never_changes_the_decision(portal_env, monkeypatch, caplog):
    class DownDynamo:
        def Table(self, name):
            raise RuntimeError("audit table down")
    # the real log_audit_event runs and swallows its own failure
    monkeypatch.setattr(su, "dynamodb", DownDynamo())
    with caplog.at_level("ERROR"):
        with pytest.raises(su.BucketNotAllowed) as info:
            su.assert_bucket_allowed(usecase_record(), FOREIGN, "p")
    assert info.value.remedy == su.REMEDY_NOT_ALLOWED
    assert any("audit table down" in r.getMessage() for r in caplog.records)
    assert su.assert_bucket_allowed(usecase_record(), "uc-one-bucket", "p") is None


# ---------------------------------------------------------------------------
# §1.5 helper binds first; role selection preserved
# ---------------------------------------------------------------------------

def _pre_change_role_choice(usecase, bucket_name):
    """The role choice as get_s3_client_for_bucket made it before this feature
    (verbatim), the oracle for _select_s3_role_for_bucket."""
    data_s3_bucket = usecase.get('data_s3_bucket')
    data_account_id = usecase.get('data_account_id')
    usecase_account_id = usecase.get('account_id')
    is_data_account_bucket = (
        data_account_id and
        data_account_id != usecase_account_id and
        bucket_name == data_s3_bucket
    )
    if is_data_account_bucket:
        return usecase.get('data_account_role_arn'), usecase.get('data_account_external_id')
    return usecase.get('cross_account_role_arn'), usecase.get('external_id')


MULTI_ACCOUNT = usecase_record(
    data_s3_bucket="data-bucket", data_account_id="acct-data",
    data_account_role_arn="arn:aws:iam::acct-data:role/DDAPortalDataAccessRole",
    data_account_external_id="ext-data",
    additional_s3_buckets=["extra-1", "extra-2"])
SAME_ACCOUNT_DATA = usecase_record(
    data_s3_bucket="data-bucket", data_account_id="acct-usecase",
    data_account_role_arn="arn:aws:iam::acct-usecase:role/Other",
    data_account_external_id="ext-data", additional_s3_buckets=["extra-1"])
SINGLE_ACCOUNT = usecase_record(
    cross_account_role_arn="arn:aws:iam::acct-portal:root", external_id=None,
    additional_s3_buckets=["extra-1"])


@pytest.mark.parametrize("record", [MULTI_ACCOUNT, SAME_ACCOUNT_DATA, SINGLE_ACCOUNT, usecase_record()])
def test_select_s3_role_for_bucket_matches_the_pre_change_choice(portal_env, record):
    for bucket in su.allowed_s3_buckets(record) | {FOREIGN}:
        assert su._select_s3_role_for_bucket(record, bucket) == _pre_change_role_choice(record, bucket)


def test_select_s3_role_for_bucket_role_table(portal_env):
    assert su._select_s3_role_for_bucket(MULTI_ACCOUNT, "data-bucket") == (
        MULTI_ACCOUNT["data_account_role_arn"], "ext-data")
    assert su._select_s3_role_for_bucket(MULTI_ACCOUNT, "uc-one-bucket") == (
        MULTI_ACCOUNT["cross_account_role_arn"], "ext-1")
    # every additional entry is reached with the cross-account role, never the data-account role
    for extra in MULTI_ACCOUNT["additional_s3_buckets"]:
        assert su._select_s3_role_for_bucket(MULTI_ACCOUNT, extra) == (
            MULTI_ACCOUNT["cross_account_role_arn"], "ext-1")
    # a data bucket in the use case's own account uses the cross-account role
    assert su._select_s3_role_for_bucket(SAME_ACCOUNT_DATA, "data-bucket") == (
        SAME_ACCOUNT_DATA["cross_account_role_arn"], "ext-1")


@pytest.mark.parametrize("record", [MULTI_ACCOUNT, SAME_ACCOUNT_DATA, SINGLE_ACCOUNT])
def test_get_s3_client_for_bucket_member_uses_the_same_role_as_before(portal_env, no_aws, quiet_audit, record):
    for bucket in sorted(su.allowed_s3_buckets(record)):
        no_aws["assume_usecase_role"].calls.clear()
        no_aws["create_boto3_client"].calls.clear()
        client = su.get_s3_client_for_bucket(record, bucket, "unit-session")
        role_arn, external_id = _pre_change_role_choice(record, bucket)
        assert no_aws["assume_usecase_role"].calls == [((role_arn, external_id, "unit-session"), {})]
        assert no_aws["create_boto3_client"].calls == [
            (("s3", no_aws["assume_usecase_role"].result), {})]
        assert client is no_aws["create_boto3_client"].result
    assert quiet_audit.calls == []


def test_get_s3_client_for_bucket_default_session_name(portal_env, no_aws, quiet_audit):
    su.get_s3_client_for_bucket(usecase_record(), "uc-one-bucket")
    assert no_aws["assume_usecase_role"].calls[0][0][2] == "portal-s3-access"


def test_get_s3_client_for_bucket_refuses_foreign_bucket_before_any_aws_call(portal_env, no_aws, quiet_audit):
    with pytest.raises(su.BucketNotAllowed) as info:
        su.get_s3_client_for_bucket(usecase_record(), FOREIGN, "training-manifest")
    assert info.value.purpose == "training-manifest"
    assert info.value.remedy == su.REMEDY_NOT_ALLOWED
    for recorder in no_aws.values():
        assert recorder.calls == []
    (_, kwargs), = quiet_audit.calls
    assert kwargs["details"] == {"bucket": FOREIGN, "purpose": "training-manifest"}
    assert kwargs["user_id"] == "system"


@pytest.mark.parametrize("owned", [ARTIFACTS, COMPONENT])
def test_get_s3_client_for_bucket_refuses_portal_owned_with_no_way_to_admit_it(
        portal_env, no_aws, quiet_audit, owned):
    for record in (usecase_record(), usecase_record(s3_bucket=owned),
                   usecase_record(additional_s3_buckets=[owned])):
        with pytest.raises(su.BucketNotAllowed) as info:
            su.get_s3_client_for_bucket(record, owned, "p")
        assert info.value.remedy == su.REMEDY_PORTAL_BUCKET
    for recorder in no_aws.values():
        assert recorder.calls == []


@pytest.mark.parametrize("bucket", [None, 42, ["uc-one-bucket"], "bad/name"])
def test_get_s3_client_for_bucket_non_member_name_is_invalid_bucket_name(portal_env, no_aws, quiet_audit, bucket):
    with pytest.raises(su.InvalidBucketName):
        su.get_s3_client_for_bucket(usecase_record(), bucket, "p")
    for recorder in no_aws.values():
        assert recorder.calls == []
    assert quiet_audit.calls == []


def test_get_s3_client_for_bucket_signature_unchanged_and_binds_first():
    import inspect
    sig = inspect.signature(su.get_s3_client_for_bucket)
    assert list(sig.parameters) == ["usecase", "bucket_name", "session_name"]
    assert sig.parameters["session_name"].default == "portal-s3-access"
    tree = ast.parse(inspect.getsource(su.get_s3_client_for_bucket))
    func = tree.body[0]
    body = [node for node in func.body if not (isinstance(node, ast.Expr)
                                               and isinstance(node.value, ast.Constant))]
    first = body[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
    assert first.value.func.id == "assert_bucket_allowed"


def test_helper_call_sites_are_unchanged():
    """The helper keeps its signature: the fourteen pre-existing call sites
    needed no edit, and the sinks bound in design §3 (model_import's
    consolidated artifact validator, data_management's four handlers, the
    pre-labeled mask probe, training's reference check and transform output)
    call it the same way — none passes a new keyword and none names the
    artifacts bucket."""
    call_sites = []
    for root in (_FUNCTIONS_DIR, _SHARED_LAYER):
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py") or name == "shared_utils.py":
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    callee = node.func
                    callee_name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", None)
                    if callee_name == "get_s3_client_for_bucket":
                        call_sites.append((name, node))
    assert len(call_sites) == 19
    assert {name for name, _ in call_sites} == {
        "dda_autolabel_worker.py", "dda_labeling.py", "dda_labeling_worker.py",
        "pre_labeled_datasets.py", "training.py", "s3_browse_utils.py",
        "data_management.py", "model_import.py"}
    for _, call in call_sites:
        assert 2 <= len(call.args) + len(call.keywords) <= 3
        assert all(kw.arg in (None, "session_name") for kw in call.keywords)


# ---------------------------------------------------------------------------
# §1.6 probe_bucket_eligibility
# ---------------------------------------------------------------------------

class ScriptedS3:
    """S3 client stub whose get_bucket_tagging plays a script of responses or
    exceptions, recording each call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def get_bucket_tagging(self, **kwargs):
        self.calls.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def probe_env(monkeypatch, portal_env):
    """assume_usecase_role / create_boto3_client / time.sleep recorders around
    a scripted S3 client; returns a function that installs a script."""
    creds = {"AccessKeyId": "k", "SecretAccessKey": "s", "SessionToken": "t"}
    sts = Recorder(result=creds)
    sleeps = Recorder()
    monkeypatch.setattr(su, "assume_usecase_role", sts)
    monkeypatch.setattr(su.time, "sleep", sleeps)
    state = {"clients": []}

    def install(script, region_script=None):
        sts.calls.clear()
        sleeps.calls.clear()
        primary = ScriptedS3(script)
        regional = ScriptedS3(region_script or [])
        factory = Recorder()

        def create(service, credentials, region=None):
            factory.calls.append(((service, credentials), {"region": region} if region else {}))
            return regional if region else primary
        monkeypatch.setattr(su, "create_boto3_client", create)
        state.update(primary=primary, regional=regional, factory=factory, sts=sts, sleeps=sleeps, creds=creds)
        return state
    return install


def test_probe_eligible_on_managed_tag(probe_env):
    env = probe_env([tagging(dda_portal__managed="true", Owner="x")])
    result = su.probe_bucket_eligibility(usecase_record(), "any-bucket")
    assert result == su.EligibilityResult(
        eligible=True, reason="eligible", tags={"dda-portal:managed": "true", "Owner": "x"},
        portal_created=False, claimed_by=None)
    assert result.shared_with_usecases == 0 and result.stale_claim is False
    assert env["primary"].calls == [{"Bucket": "any-bucket"}]
    assert env["sts"].calls == [((usecase_record()["cross_account_role_arn"], "ext-1",
                                 su.ELIGIBILITY_SESSION_NAME), {})]
    assert env["factory"].calls == [(("s3", env["creds"]), {})]
    assert env["sleeps"].calls == []


def test_probe_eligible_on_created_by_portal_with_claim(probe_env):
    probe_env([tagging(dda_portal__created_by="portal", dda_portal__usecase_id="uc-9")])
    result = su.probe_bucket_eligibility(usecase_record(), "any-bucket")
    assert result.eligible and result.reason == "eligible"
    assert result.portal_created is True and result.claimed_by == "uc-9"


def test_probe_uses_the_data_account_role_for_the_data_bucket(probe_env):
    env = probe_env([tagging(dda_portal__managed="true")])
    su.probe_bucket_eligibility(MULTI_ACCOUNT, "data-bucket")
    assert env["sts"].calls[0][0][:2] == (MULTI_ACCOUNT["data_account_role_arn"], "ext-data")
    env["sts"].calls.clear()
    probe_env([tagging(dda_portal__managed="true")])
    su.probe_bucket_eligibility(MULTI_ACCOUNT, "extra-1")
    assert env["sts"].calls[0][0][:2] == (MULTI_ACCOUNT["cross_account_role_arn"], "ext-1")


@pytest.mark.parametrize("script, reason, tags", [
    ([tagging(Owner="x", dda_portal__managed="false")], "tag_missing", {"Owner": "x", "dda-portal:managed": "false"}),
    ([tagging(dda_portal__created_by="console")], "tag_missing", {"dda-portal:created-by": "console"}),
    ([{"TagSet": []}], "tag_missing", {}),
    ([client_error("NoSuchTagSet", 404)], "no_tags", {}),
    ([client_error("AccessDenied", 403)], "tagging_unreadable", {}),
    ([client_error("SomethingElse", 403)], "tagging_unreadable", {}),
    ([client_error("NoSuchBucket", 404)], "bucket_not_found", {}),
    ([client_error("SomethingElse", 404)], "bucket_not_found", {}),
    ([client_error("InternalError", 500)], "credentials_unavailable", {}),
    ([client_error("PermanentRedirect", 301)], "tagging_unreadable", {}),
    ([RuntimeError("boom")], "credentials_unavailable", {}),
])
def test_probe_outcome_table(probe_env, script, reason, tags):
    env = probe_env(script)
    result = su.probe_bucket_eligibility(usecase_record(), "any-bucket")
    assert (result.eligible, result.reason, result.tags) == (False, reason, tags)
    assert result.portal_created is False and result.claimed_by is None
    assert len(env["primary"].calls) == 1
    assert env["sleeps"].calls == []


def test_probe_sts_failure_is_credentials_unavailable(probe_env, monkeypatch):
    env = probe_env([tagging(dda_portal__managed="true")])
    monkeypatch.setattr(su, "assume_usecase_role",
                        Recorder(raises=client_error("AccessDenied", 403, "AssumeRole")))
    result = su.probe_bucket_eligibility(usecase_record(), "any-bucket")
    assert (result.eligible, result.reason, result.tags) == (False, "credentials_unavailable", {})
    assert env["primary"].calls == []
    monkeypatch.setattr(su, "assume_usecase_role", Recorder(raises=RuntimeError("no network")))
    assert su.probe_bucket_eligibility(usecase_record(), "any-bucket").reason == "credentials_unavailable"


def test_probe_without_role_fields_is_credentials_unavailable(probe_env, monkeypatch):
    probe_env([tagging(dda_portal__managed="true")])
    monkeypatch.setattr(su, "create_boto3_client", Recorder(raises=TypeError("bad credentials")))
    result = su.probe_bucket_eligibility({"usecase_id": "uc-x"}, "any-bucket")
    assert result.reason == "credentials_unavailable" and not result.eligible


def test_probe_retries_only_when_asked(probe_env):
    script = [client_error("NoSuchTagSet", 404), client_error("NoSuchTagSet", 404),
              tagging(dda_portal__managed="true")]
    env = probe_env(list(script))
    result = su.probe_bucket_eligibility(usecase_record(), "fresh-bucket", retry=True)
    assert result.eligible and result.reason == "eligible"
    assert len(env["primary"].calls) == 3
    assert env["sleeps"].calls == [((su.ELIGIBILITY_RETRY_DELAY_SECONDS,), {})] * 2
    assert len(env["sts"].calls) == 1          # one STS call for the whole probe

    env = probe_env(list(script))
    result = su.probe_bucket_eligibility(usecase_record(), "fresh-bucket")
    assert (result.eligible, result.reason) == (False, "no_tags")
    assert len(env["primary"].calls) == 1
    assert env["sleeps"].calls == []


def test_probe_retry_also_covers_tag_missing_and_is_bounded(probe_env):
    env = probe_env([tagging(Owner="x"), tagging(Owner="x"), tagging(dda_portal__managed="true")])
    assert su.probe_bucket_eligibility(usecase_record(), "b", retry=True).eligible
    assert len(env["sleeps"].calls) == 2

    env = probe_env([client_error("NoSuchTagSet", 404)] * 3)
    result = su.probe_bucket_eligibility(usecase_record(), "b", retry=True)
    assert result.reason == "no_tags"
    assert len(env["primary"].calls) == su.ELIGIBILITY_RETRY_ATTEMPTS == 3
    assert len(env["sleeps"].calls) == 2

    env = probe_env([tagging(Owner="x")] * 3)
    assert su.probe_bucket_eligibility(usecase_record(), "b", retry=True).reason == "tag_missing"
    assert len(env["primary"].calls) == 3


@pytest.mark.parametrize("error, reason", [
    (client_error("AccessDenied", 403), "tagging_unreadable"),
    (client_error("NoSuchBucket", 404), "bucket_not_found"),
    (client_error("InternalError", 500), "credentials_unavailable"),
])
def test_probe_retry_never_repeats_conclusive_failures(probe_env, error, reason):
    env = probe_env([error, tagging(dda_portal__managed="true")])
    assert su.probe_bucket_eligibility(usecase_record(), "b", retry=True).reason == reason
    assert len(env["primary"].calls) == 1 and env["sleeps"].calls == []


def test_probe_follows_permanent_redirect_once(probe_env):
    redirect = client_error("PermanentRedirect", 301, headers={"x-amz-bucket-region": "eu-west-1"})
    env = probe_env([redirect], region_script=[tagging(dda_portal__managed="true")])
    result = su.probe_bucket_eligibility(usecase_record(), "eu-bucket")
    assert result.eligible and result.reason == "eligible"
    assert env["primary"].calls == [{"Bucket": "eu-bucket"}]
    assert env["regional"].calls == [{"Bucket": "eu-bucket"}]
    assert env["factory"].calls == [(("s3", env["creds"]), {}),
                                    (("s3", env["creds"]), {"region": "eu-west-1"})]
    assert len(env["sts"].calls) == 1          # same credentials, no second STS call
    assert env["sleeps"].calls == []


def test_probe_redirect_without_region_header_is_tagging_unreadable(probe_env):
    env = probe_env([client_error("PermanentRedirect", 301, headers={"server": "AmazonS3"})])
    result = su.probe_bucket_eligibility(usecase_record(), "eu-bucket")
    assert (result.eligible, result.reason) == (False, "tagging_unreadable")
    assert len(env["factory"].calls) == 1


def test_probe_redirect_second_failure_is_tagging_unreadable(probe_env):
    redirect = client_error("PermanentRedirect", 301, headers={"x-amz-bucket-region": "eu-west-1"})
    for second in (client_error("PermanentRedirect", 301, headers={"x-amz-bucket-region": "us-west-2"}),
                   client_error("AccessDenied", 403),
                   client_error("NoSuchBucket", 404)):
        env = probe_env([redirect], region_script=[second])
        result = su.probe_bucket_eligibility(usecase_record(), "eu-bucket")
        assert result.reason == "tagging_unreadable"
        assert len(env["factory"].calls) == 2


def test_probe_redirect_by_http_status_alone(probe_env):
    redirect = client_error("301", 301, headers={"x-amz-bucket-region": "ap-south-1"})
    env = probe_env([redirect], region_script=[client_error("NoSuchTagSet", 404)])
    result = su.probe_bucket_eligibility(usecase_record(), "ap-bucket")
    assert result.reason == "no_tags"      # a clean read on the region-bound client concludes normally
    assert env["factory"].calls[1] == (("s3", env["creds"]), {"region": "ap-south-1"})


def test_probe_logs_reason_at_info_and_never_raises(probe_env, caplog):
    probe_env([client_error("AccessDenied", 403)])
    with caplog.at_level("INFO"):
        su.probe_bucket_eligibility(usecase_record(), "b")
    assert any("eligibility: tagging_unreadable" in r.getMessage() and r.levelname == "INFO"
               for r in caplog.records)


# ---------------------------------------------------------------------------
# §1.6 check_bucket_attachable
# ---------------------------------------------------------------------------

class FakeTable:
    """usecases table stub: `scan` serves `scan_pages` in order (with a
    LastEvaluatedKey on every page but the last, then starts over), `get_item`
    answers from `items`."""

    def __init__(self, scan_pages=None, items=None, scan_error=None, get_error=None):
        self.scan_pages = list(scan_pages or [[]])
        self.items = set(items or [])
        self.scan_error = scan_error
        self.get_error = get_error
        self.scan_calls = []
        self.get_calls = []
        self._page = 0

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        if self.scan_error:
            raise self.scan_error
        page = self.scan_pages[self._page]
        response = {"Items": [{"usecase_id": uid} for uid in page]}
        if self._page < len(self.scan_pages) - 1:
            response["LastEvaluatedKey"] = {"usecase_id": page[-1] if page else "k"}
            self._page += 1
        else:
            self._page = 0
        return response

    def get_item(self, **kwargs):
        self.get_calls.append(kwargs)
        if self.get_error:
            raise self.get_error
        key = kwargs["Key"]["usecase_id"]
        return {"Item": {"usecase_id": key}} if key in self.items else {}


@pytest.fixture
def attach_env(monkeypatch, portal_env, quiet_audit):
    """Stubbed table, probe and PortalAdmin gate for check_bucket_attachable."""
    state = {"audit": quiet_audit}

    def install(*, scan_pages=None, existing=(), probe_result=None, admin=False,
                scan_error=None, get_error=None, admin_error=None):
        quiet_audit.calls.clear()
        table = FakeTable(scan_pages=scan_pages, items=existing, scan_error=scan_error, get_error=get_error)
        monkeypatch.setattr(su, "dynamodb", type("DDB", (), {"Table": staticmethod(lambda name: table)})())
        monkeypatch.setattr(su, "USECASES_TABLE", "test-usecases")
        # attribution_from reads the registry for the identity source; keep the
        # table recorder to the validator's own Scan / get_item
        monkeypatch.setattr(su, "attribution_from", Recorder(result={
            "username": "u", "email": "e", "source_ip": "10.0.0.1",
            "user_agent": "ua", "identity_source": "registry"}))
        probe = Recorder(result=probe_result or su.EligibilityResult(
            eligible=True, reason="eligible", tags={"dda-portal:managed": "true"},
            portal_created=False, claimed_by=None))
        monkeypatch.setattr(su, "probe_bucket_eligibility", probe)
        gate = Recorder(result=admin, raises=admin_error)
        monkeypatch.setattr(su, "caller_is_portal_admin", gate)
        state.update(table=table, probe=probe, gate=gate)
        return state
    return install


USER = {"user_id": "user-1", "username": "user-1"}
EVENT = {"requestContext": {"identity": {"sourceIp": "10.0.0.1"}}}


def attach(bucket, *, target="uc-1", user=USER, evidence=None, retry=False, record=None):
    return su.check_bucket_attachable(
        target_usecase_id=target, usecase_like=record or usecase_record(), bucket=bucket,
        user=user, event=EVENT, purpose="usecase-update", evidence=evidence, retry=retry)


def test_attach_admits_an_eligible_unclaimed_bucket(attach_env):
    env = attach_env(scan_pages=[["uc-1"]])          # only the target itself names it
    result = attach("new-bucket")
    assert result.eligible and result.shared_with_usecases == 0 and result.stale_claim is False
    assert env["probe"].calls == [((usecase_record(), "new-bucket"), {"retry": False})]
    assert env["table"].scan_calls == [{
        "ProjectionExpression": "usecase_id",
        "FilterExpression": "s3_bucket = :b OR data_s3_bucket = :b OR contains(additional_s3_buckets, :b)",
        "ExpressionAttributeValues": {":b": "new-bucket"},
    }]
    assert env["table"].get_calls == []
    assert env["gate"].calls == []                   # no sharing question, no registry read
    assert env["audit"].calls == []


def test_attach_passes_retry_through_to_the_probe(attach_env):
    env = attach_env()
    attach("new-bucket", retry=True)
    assert env["probe"].calls[0][1] == {"retry": True}
    attach("new-bucket")
    assert env["probe"].calls[1][1] == {"retry": False}


@pytest.mark.parametrize("owned", [ARTIFACTS, COMPONENT])
@pytest.mark.parametrize("admin", [False, True])
def test_attach_refuses_portal_owned_before_scan_and_probe(attach_env, owned, admin):
    env = attach_env(admin=admin)                    # the stubbed probe would say managed=true
    with pytest.raises(su.BucketNotEligible) as info:
        attach(owned)
    exc = info.value
    assert (exc.error_code, exc.reason, exc.remedy) == ("bucket_not_eligible", "portal_bucket", su.REMEDY_PORTAL_BUCKET)
    assert exc.bucket == owned and exc.usecase_id == "uc-1" and exc.purpose == "usecase-update"
    assert env["table"].scan_calls == [] and env["probe"].calls == [] and env["gate"].calls == []
    (_, kwargs), = env["audit"].calls
    assert kwargs["action"] == "s3_bucket_refused" and kwargs["result"] == "denied"
    assert kwargs["details"] == {"bucket": owned, "purpose": "usecase-update", "reason": "portal_bucket"}
    assert kwargs["resource_id"] == "uc-1" and kwargs["user_id"] == "user-1"


@pytest.mark.parametrize("bucket", [None, 42, ["b"], {"b": 1}, "ab", "bad/name", "x y", ""])
def test_attach_non_string_or_malformed_bucket_is_invalid_bucket_name(attach_env, bucket):
    env = attach_env()
    with pytest.raises(su.InvalidBucketName) as info:
        attach(bucket)
    assert info.value.error_code == "invalid_bucket_name"
    assert info.value.usecase_id == "uc-1"
    assert env["table"].scan_calls == [] and env["probe"].calls == []
    assert env["audit"].calls == []


def test_attach_refuses_bucket_configured_on_another_usecase(attach_env):
    env = attach_env(scan_pages=[["uc-other", "uc-1"]])
    with pytest.raises(su.BucketNotEligible) as info:
        attach("shared-bucket")
    assert (info.value.reason, info.value.remedy) == ("bucket_configured_on_other_usecase", su.REMEDY_OTHER_USECASE)
    assert "uc-other" not in info.value.to_body().values()
    assert env["probe"].calls == []                  # refused before the probe
    assert env["gate"].calls == [((USER,), {})]
    (_, kwargs), = env["audit"].calls
    assert kwargs["details"] == {"bucket": "shared-bucket", "purpose": "usecase-update",
                                 "reason": "bucket_configured_on_other_usecase"}


def test_attach_scan_is_paginated(attach_env):
    env = attach_env(scan_pages=[["uc-a"], [], ["uc-1"]], admin=True)
    result = attach("shared-bucket")
    assert len(env["table"].scan_calls) == 3
    assert "ExclusiveStartKey" not in env["table"].scan_calls[0]
    assert env["table"].scan_calls[1]["ExclusiveStartKey"] == {"usecase_id": "uc-a"}
    assert env["table"].scan_calls[2]["ExclusiveStartKey"] == {"usecase_id": "k"}
    assert result.shared_with_usecases == 1


def test_attach_portal_admin_override_reports_sharing(attach_env):
    env = attach_env(scan_pages=[["uc-a", "uc-b", "uc-1"]], admin=True)
    result = attach("shared-bucket")
    assert result.eligible
    assert result.shared_with_usecases == 2 and result.stale_claim is False
    assert env["probe"].calls and env["audit"].calls == []


def test_attach_probe_refusal_carries_reason_and_remedy(attach_env):
    for reason, remedy in su.REMEDY_FOR_REASON.items():
        if reason in ("portal_bucket", "account_mismatch", "too_many_buckets"):
            continue
        env = attach_env(probe_result=su.EligibilityResult(
            eligible=False, reason=reason, tags={}, portal_created=False, claimed_by=None))
        with pytest.raises(su.BucketNotEligible) as info:
            attach("some-bucket")
        assert (info.value.reason, info.value.remedy) == (reason, remedy)
        (_, kwargs), = env["audit"].calls
        assert kwargs["details"]["reason"] == reason
        env["audit"].calls.clear()


def test_attach_claim_by_existing_usecase(attach_env):
    claimed = su.EligibilityResult(eligible=True, reason="eligible",
                                   tags={"dda-portal:created-by": "portal", "dda-portal:usecase-id": "uc-other"},
                                   portal_created=True, claimed_by="uc-other")
    env = attach_env(probe_result=claimed, existing=["uc-other"])
    with pytest.raises(su.BucketNotEligible) as info:
        attach("claimed-bucket")
    assert (info.value.reason, info.value.remedy) == ("bucket_configured_on_other_usecase", su.REMEDY_OTHER_USECASE)
    assert env["table"].get_calls == [{"Key": {"usecase_id": "uc-other"}, "ProjectionExpression": "usecase_id"}]
    assert env["gate"].calls == [((USER,), {})]

    env = attach_env(probe_result=claimed, existing=["uc-other"], admin=True)
    result = attach("claimed-bucket")
    assert result is claimed
    assert result.shared_with_usecases == 1 and result.stale_claim is False
    assert len(env["table"].get_calls) == 1


def test_attach_stale_claim_refused_then_admitted_for_portal_admin(attach_env):
    stale = su.EligibilityResult(eligible=True, reason="eligible",
                                 tags={"dda-portal:managed": "true", "dda-portal:usecase-id": "uc-deleted"},
                                 portal_created=False, claimed_by="uc-deleted")
    env = attach_env(probe_result=stale, existing=[])  # get_item finds no such use case
    with pytest.raises(su.BucketNotEligible) as info:
        attach("orphaned-bucket")
    assert (info.value.reason, info.value.remedy) == ("bucket_configured_on_other_usecase", su.REMEDY_STALE_CLAIM)
    (_, kwargs), = env["audit"].calls
    assert kwargs["details"]["reason"] == "bucket_configured_on_other_usecase"

    env = attach_env(probe_result=stale, existing=[], admin=True)
    result = attach("orphaned-bucket")
    assert result.stale_claim is True and result.shared_with_usecases == 1
    assert env["audit"].calls == []


def test_attach_own_claim_is_not_a_claim(attach_env):
    own = su.EligibilityResult(eligible=True, reason="eligible",
                               tags={"dda-portal:usecase-id": "uc-1", "dda-portal:managed": "true"},
                               portal_created=False, claimed_by="uc-1")
    env = attach_env(probe_result=own)
    result = attach("my-created-bucket")
    assert result.shared_with_usecases == 0 and result.stale_claim is False
    assert env["table"].get_calls == [] and env["gate"].calls == []


def test_attach_claim_already_counted_in_configuration_is_not_double_counted(attach_env):
    claimed = su.EligibilityResult(eligible=True, reason="eligible",
                                   tags={"dda-portal:usecase-id": "uc-a"}, portal_created=False, claimed_by="uc-a")
    env = attach_env(scan_pages=[["uc-a"]], probe_result=claimed, existing=["uc-a"], admin=True)
    result = attach("shared-bucket")
    assert result.shared_with_usecases == 1
    assert len(env["gate"].calls) == 1               # the PortalAdmin answer is resolved once


def test_attach_evidence_replaces_the_probe(attach_env):
    env = attach_env()
    evidence = su.EligibilityResult(eligible=True, reason="eligible",
                                    tags={"dda-portal:managed": "true", "dda-portal:created-by": "portal",
                                          "dda-portal:usecase-id": "uc-1"},
                                    portal_created=True, claimed_by="uc-1")
    result = attach("just-created", evidence=evidence)
    assert result is evidence
    assert env["probe"].calls == []
    assert result.shared_with_usecases == 0 and result.stale_claim is False


def test_attach_scan_error_propagates(attach_env):
    env = attach_env(scan_error=client_error("ProvisionedThroughputExceededException", 400, "Scan"))
    with pytest.raises(ClientError):
        attach("some-bucket")
    assert env["probe"].calls == [] and env["audit"].calls == []


def test_attach_get_item_error_propagates(attach_env):
    claimed = su.EligibilityResult(eligible=True, reason="eligible",
                                   tags={"dda-portal:usecase-id": "uc-other"}, portal_created=False, claimed_by="uc-other")
    env = attach_env(probe_result=claimed, get_error=client_error("InternalServerError", 500, "GetItem"))
    with pytest.raises(ClientError):
        attach("claimed-bucket")
    assert env["audit"].calls == []


def test_attach_registry_unavailable_propagates(attach_env):
    env = attach_env(scan_pages=[["uc-other"]], admin_error=su.RegistryUnavailable("registry down"))
    with pytest.raises(su.RegistryUnavailable):
        attach("shared-bucket")
    assert env["audit"].calls == []


def test_attach_audit_row_uses_the_target_usecase_and_actor(attach_env, monkeypatch):
    env = attach_env(probe_result=su.EligibilityResult(
        eligible=False, reason="no_tags", tags={}, portal_created=False, claimed_by=None))
    attribution = Recorder(result={"username": "u", "email": "e", "source_ip": "10.0.0.1",
                                   "user_agent": "ua", "identity_source": "registry"})
    monkeypatch.setattr(su, "attribution_from", attribution)
    with pytest.raises(su.BucketNotEligible):
        attach("untagged", target="uc-new", record={"account_id": "acct-usecase"})
    (_, kwargs), = env["audit"].calls
    assert kwargs == {
        "user_id": "user-1", "action": "s3_bucket_refused", "resource_type": "usecase",
        "resource_id": "uc-new", "result": "denied",
        "details": {"bucket": "untagged", "purpose": "usecase-update", "reason": "no_tags"},
        "identity": attribution.result,
    }
    assert attribution.calls == [((EVENT, USER, "uc-new"), {})]


def test_eligibility_result_defaults():
    result = su.EligibilityResult(eligible=False, reason="no_tags", tags={}, portal_created=False, claimed_by=None)
    assert result.shared_with_usecases == 0 and result.stale_claim is False
    assert [f for f in su.EligibilityResult.__dataclass_fields__] == [
        "eligible", "reason", "tags", "portal_created", "claimed_by", "shared_with_usecases", "stale_claim"]


# ---------------------------------------------------------------------------
# §2.4 writer guard (Requirement 10.10)
# ---------------------------------------------------------------------------

WRITE_CALLS = {"put_item", "update_item", "batch_writer", "transact_write_items"}
BUCKET_ATTRIBUTES = {"s3_bucket", "data_s3_bucket", "additional_s3_buckets"}
UPDATE_EXPRESSION_TOKENS = ("SET ", "REMOVE ", "list_append", "contains(", "attribute_not_exists(")


def _call_name(node: ast.Call):
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _is_bucket_attribute(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in BUCKET_ATTRIBUTES


def _names_bucket_attribute_in_write_position(func: ast.AST) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if _is_bucket_attribute(key):                       # dict-literal key
                    return True
                if (isinstance(key, ast.Constant) and isinstance(key.value, str)
                        and key.value.startswith("#") and _is_bucket_attribute(value)):
                    return True                                      # ExpressionAttributeNames alias
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            if any(_is_bucket_attribute(element) for element in node.elts):
                return True                                          # field list
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Subscript) and _is_bucket_attribute(target.slice):
                    return True                                      # subscript store
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if (any(attribute in text for attribute in BUCKET_ATTRIBUTES)
                    and any(token in text for token in UPDATE_EXPRESSION_TOKENS)):
                return True                                          # update-expression string
    return False


def _contains_write_call(func: ast.AST) -> bool:
    return any(isinstance(node, ast.Call) and _call_name(node) in WRITE_CALLS for node in ast.walk(func))


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def bucket_attribute_writers(path: str):
    """Module-qualified names of the functions in `path` the guard rule flags."""
    module = os.path.splitext(os.path.basename(path))[0]
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    return {f"{module}.{func.name}" for func in _functions(tree)
            if _contains_write_call(func) and _names_bucket_attribute_in_write_position(func)}


def helper_call_sites(path: str, helper_names):
    """Module-qualified names of the functions in `path` that call one of `helper_names`."""
    module = os.path.splitext(os.path.basename(path))[0]
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    return {f"{module}.{func.name}" for func in _functions(tree)
            if any(isinstance(node, ast.Call) and _call_name(node) in helper_names
                   for node in ast.walk(func))}


def _python_files(*roots):
    for root in roots:
        for name in sorted(os.listdir(root)):
            if name.endswith(".py"):
                yield os.path.join(root, name)


def test_only_enumerated_writers_touch_bucket_attributes():
    """Every function in functions/*.py and the shared layer that writes
    DynamoDB and names a bucket attribute in a write-shaping position is one
    of the five enumerated writers (design §2.4, §12): the three record
    handlers that call `check_bucket_attachable` before their write and the
    two additional-list helpers, which are called only from `update_usecase`
    and `create_s3_bucket`. A new writer anywhere else fails this test.
    """
    writers = set()
    for path in _python_files(_FUNCTIONS_DIR, _SHARED_LAYER):
        writers |= bucket_attribute_writers(path)
    assert writers == {
        "usecases.create_usecase",
        "usecases.update_usecase",
        "usecases._add_additional_bucket",
        "usecases._remove_additional_bucket",
        "data_management.configure_data_account",
    }
    callers = set()
    for path in _python_files(_FUNCTIONS_DIR, _SHARED_LAYER):
        callers |= helper_call_sites(path, {"_add_additional_bucket", "_remove_additional_bucket"})
    assert callers == {"usecases.update_usecase", "usecases.create_s3_bucket"}


def test_writer_guard_rule_detects_each_shape():
    writers = bucket_attribute_writers(_FIXTURE_MODULE)
    assert writers == {
        "bucket_attribute_writers.writes_dict_key",
        "bucket_attribute_writers.writes_field_list",
        "bucket_attribute_writers.writes_tuple_element",
        "bucket_attribute_writers.writes_subscript_store",
        "bucket_attribute_writers.writes_expression_attribute_alias",
        "bucket_attribute_writers.writes_update_expression_string",
        "bucket_attribute_writers.writes_through_batch_writer",
        "bucket_attribute_writers.writes_through_transact",
    }
    for must_not_match in ("reads_bucket_and_writes_elsewhere", "shapes_response_without_writing",
                           "filters_by_bucket_without_writing"):
        assert f"bucket_attribute_writers.{must_not_match}" not in writers


def test_writer_guard_rule_requires_both_a_write_call_and_a_write_position():
    with open(_FIXTURE_MODULE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    by_name = {func.name: func for func in _functions(tree)}
    reader = by_name["reads_bucket_and_writes_elsewhere"]
    assert _contains_write_call(reader) and not _names_bucket_attribute_in_write_position(reader)
    shaper = by_name["shapes_response_without_writing"]
    assert _names_bucket_attribute_in_write_position(shaper) and not _contains_write_call(shaper)
    scanner = by_name["filters_by_bucket_without_writing"]
    assert _names_bucket_attribute_in_write_position(scanner) and not _contains_write_call(scanner)
