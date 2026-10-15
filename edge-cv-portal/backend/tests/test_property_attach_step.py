"""
Property test for the Attach_Step (`attach_bucket`) of the shared layer
(portal-authorization-and-iam-scoping, tasks.md 2.1; design I5).

The oracle is written from the inputs and never calls the implementation:
PutBucketAbac is due iff the validator passed, the bucket is a
Portal_Account_Bucket (the selector's role ARN is the account root), the
bucket carries dda-portal:managed=true and its ABAC status is not 'Enabled'.
The five guarded calls (the region HeadBucket, GetBucketAbac, PutBucketAbac,
the probe's HeadBucket and its one-key ListObjectsV2) each raise a generated
exception with non-trivial probability; whatever they raise, the Attach
returns normally once the validator has passed.

Validates: Requirements 7.6, 7.11
"""
import os
from types import SimpleNamespace

from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError
from hypothesis import given, settings
from hypothesis import strategies as st

import shared_utils as su

PORTAL = "111111111111"
USECASE = "222222222222"
DATA = "333333333333"
BUCKET = "property-bucket"
DATA_BUCKET = "property-data-bucket"
GUARDED_CALLS = ("region", "get_bucket_abac", "put_bucket_abac", "probe_head", "probe_list")
USER = {"user_id": "user-property"}


def _client_error(code, status, headers=None):
    return ClientError({"Error": {"Code": code, "Message": code},
                        "ResponseMetadata": {"HTTPStatusCode": status,
                                             "HTTPHeaders": headers or {}}}, "Operation")


EXCEPTIONS = st.sampled_from([
    lambda: _client_error("AccessDenied", 403),
    lambda: _client_error("403", 403),
    lambda: _client_error("NoSuchBucket", 404),
    lambda: _client_error("PermanentRedirect", 301),
    lambda: _client_error("InternalError", 500),
    lambda: _client_error("SlowDown", 503),
    lambda: EndpointConnectionError(endpoint_url="fake-endpoint"),
    lambda: ConnectTimeoutError(endpoint_url="fake-endpoint"),
    lambda: su.RegionUnknown("no header"),
    lambda: RuntimeError("unexpected"),
    lambda: ValueError("unexpected"),
    lambda: KeyError("Status"),
    lambda: TypeError("unexpected"),
])

RECORDS = st.sampled_from(["single", "multi", "data_account_bucket", "data_account_primary",
                           "legacy_no_arn"])
TAGS = st.sampled_from([
    {"dda-portal:managed": "true"},
    {"dda-portal:managed": "true", "dda-portal:created-by": "portal"},
    {"dda-portal:created-by": "portal"},
    {"dda-portal:managed": "false", "dda-portal:created-by": "portal"},
    {"dda-portal:managed": "TRUE"},
    {},
])
STATUSES = st.sampled_from(["Enabled", "Disabled", None, "", "enabled"])


def _record(kind):
    single = {"usecase_id": "uc-p", "account_id": PORTAL,
              "cross_account_role_arn": f"arn:aws:iam::{PORTAL}:root", "s3_bucket": BUCKET}
    if kind == "single":
        return single, BUCKET, True
    if kind == "multi":
        return ({"usecase_id": "uc-p", "account_id": USECASE,
                 "cross_account_role_arn": f"arn:aws:iam::{USECASE}:role/DDAPortalAccessRole",
                 "external_id": "ext", "s3_bucket": BUCKET}, BUCKET, False)
    with_data = dict(single, data_account_id=DATA,
                     data_account_role_arn=f"arn:aws:iam::{DATA}:role/DDAPortalDataAccessRole",
                     data_account_external_id="ext-data", data_s3_bucket=DATA_BUCKET)
    if kind == "data_account_bucket":
        return with_data, DATA_BUCKET, False
    if kind == "data_account_primary":
        return with_data, BUCKET, True
    return {"usecase_id": "uc-p", "account_id": PORTAL, "s3_bucket": BUCKET}, BUCKET, False


class PlannedS3:
    """One S3 fake for every region and role; each guarded call raises the
    planned exception the first time it runs (when one is planned)."""

    def __init__(self, status, failures, allow_probe):
        self.status = status
        self.failures = dict(failures)
        self.allow_probe = allow_probe
        self.calls = []
        self.resolved = False

    def _fail(self, name):
        failure = self.failures.pop(name, None)
        if failure is not None:
            raise failure

    def head_bucket(self, **kwargs):
        # The first HeadBucket of a tagged Portal_Account_Bucket is the region
        # resolver's; every later one is the probe's.
        name = "region" if self.expect_resolver and not self.resolved else "probe_head"
        self.resolved = True
        self.calls.append(name)
        self._fail(name)
        if name == "probe_head" and not self.allow_probe:
            raise _client_error("403", 403, {"x-amz-bucket-region": "us-east-1"})
        return {"ResponseMetadata": {"HTTPStatusCode": 200,
                                     "HTTPHeaders": {"x-amz-bucket-region": "us-east-1"}}}

    def get_bucket_abac(self, **kwargs):
        self.calls.append("get_bucket_abac")
        self._fail("get_bucket_abac")
        return {"AbacStatus": {} if self.status is None else {"Status": self.status}}

    def put_bucket_abac(self, **kwargs):
        self.calls.append("put_bucket_abac")
        self._fail("put_bucket_abac")
        self.status = "Enabled"

    def list_objects_v2(self, **kwargs):
        self.calls.append("probe_list")
        self._fail("probe_list")
        if not self.allow_probe:
            raise _client_error("AccessDenied", 403)
        return {"KeyCount": 0}


@st.composite
def scenarios(draw):
    kind = draw(RECORDS)
    failures = {}
    for name in GUARDED_CALLS:
        if draw(st.booleans()) and draw(st.booleans()):         # about one call in four
            failures[name] = draw(EXCEPTIONS)()
    return SimpleNamespace(
        kind=kind, tags=draw(TAGS), status=draw(STATUSES), failures=failures,
        validator_passes=draw(st.integers(0, 9)) > 0, allow_probe=draw(st.booleans()),
        scoping=draw(st.sampled_from(["tag", "tag-objects-only"])))


class _Patched:
    """Install the recording seams on shared_utils for one example and restore
    them afterwards (hypothesis examples cannot use function-scoped fixtures)."""

    def __init__(self, **replacements):
        self.replacements = replacements
        self.saved = {}

    def __enter__(self):
        for name, value in self.replacements.items():
            self.saved[name] = getattr(su, name)
            setattr(su, name, value)
        self.saved_sleep = su.time.sleep
        return self

    def __exit__(self, *exc):
        for name, value in self.saved.items():
            setattr(su, name, value)
        su.time.sleep = self.saved_sleep
        return False


class TestAttachStepProperty:
    """**Feature: portal-authorization-and-iam-scoping, Property 10: the
    Attach_Step enables ABAC exactly when due and never raises after the
    validator** — PutBucketAbac is called iff the validator passed, the
    bucket is a Portal_Account_Bucket, it carries dda-portal:managed=true and
    its status is not 'Enabled' (when the region read and the status read
    succeed; it is never called otherwise), and `attach_bucket` returns
    normally for any exception from any of the five guarded calls.

    **Validates: Requirements 7.6, 7.11**
    """

    @settings(max_examples=150, deadline=None)
    @given(scenario=scenarios())
    def test_abac_enable_iff_due_and_no_raise_after_the_validator(self, scenario):
        usecase, bucket, portal_account = _record(scenario.kind)
        tagged = scenario.tags.get("dda-portal:managed") == "true"
        s3 = PlannedS3(scenario.status, scenario.failures, scenario.allow_probe)
        s3.expect_resolver = portal_account and tagged
        factory_calls, audit_calls, sleeps, escaped, sessions = [], [], [], [], []
        refusal = su.BucketNotEligible("not eligible", bucket=bucket, reason="tag_missing")

        def factory(service_name, credentials, region=None):
            factory_calls.append((service_name, region))
            return s3

        def assume(role_arn, external_id, session_name):
            sessions.append(session_name)
            if role_arn is None:
                raise ValueError("no role")
            return {"AccessKeyId": "test", "SecretAccessKey": "test", "SessionToken": "test"}

        def validator(**kwargs):
            if not scenario.validator_passes:
                raise refusal
            return su.EligibilityResult(eligible=True, reason="eligible", tags=dict(scenario.tags),
                                        portal_created=False, claimed_by=None)

        fake_boto3 = SimpleNamespace(client=lambda *a, **k: escaped.append(a) or s3,
                                     resource=su.boto3.resource)
        old_scoping = os.environ.get("DATA_PLANE_SCOPING")
        os.environ["DATA_PLANE_SCOPING"] = scenario.scoping
        try:
            with _Patched(create_boto3_client=factory, assume_usecase_role=assume,
                          check_bucket_attachable=validator,
                          log_audit_event=lambda *a, **k: audit_calls.append((a, k)),
                          boto3=fake_boto3):
                su.time.sleep = lambda seconds: sleeps.append(seconds)
                try:
                    outcome = su.attach_bucket(
                        target_usecase_id="uc-p", usecase_like=usecase, bucket=bucket, user=USER,
                        event=None, purpose="s3_bucket", source="api")
                except su.BucketNotEligible as error:
                    assert not scenario.validator_passes and error is refusal
                    assert factory_calls == [] and s3.calls == [] and audit_calls == []
                    return
        finally:
            if old_scoping is None:
                os.environ.pop("DATA_PLANE_SCOPING", None)
            else:
                os.environ["DATA_PLANE_SCOPING"] = old_scoping

        assert scenario.validator_passes
        assert escaped == []
        assert set(sessions) <= {su.ATTACH_SESSION_NAME}
        assert bool(sessions) is not portal_account
        put_called = "put_bucket_abac" in s3.calls
        due = portal_account and tagged and scenario.status != "Enabled"
        if put_called:
            assert due
        if "region" not in scenario.failures and "get_bucket_abac" not in scenario.failures:
            assert put_called == due
        assert s3.calls.count("put_bucket_abac") <= 1

        assert outcome.tagged is tagged
        if not portal_account:
            assert (outcome.abac_enabled, outcome.abac_action) == ("unknown", "not_applicable")
            assert not {"region", "get_bucket_abac", "put_bucket_abac"} & set(s3.calls)
        elif not tagged:
            assert (outcome.abac_enabled, outcome.abac_action) == (False, "tag_missing")
        else:
            assert outcome.abac_action in ("already_enabled", "enabled", "enable_failed")
            assert outcome.abac_enabled is (outcome.abac_action != "enable_failed")
        abac_audits = [call for call in audit_calls if call[0][1] == "bucket_abac_enabled"]
        expected_audits = 1 if portal_account and tagged and outcome.abac_action != "already_enabled" else 0
        assert len(abac_audits) == expected_audits
        assert (outcome.remedy is None) is outcome.iam_reachable
        assert sleeps in ([], [su.ATTACH_PROBE_RETRY_DELAY_SECONDS])
        if sleeps:
            assert outcome.abac_action == "enabled"
