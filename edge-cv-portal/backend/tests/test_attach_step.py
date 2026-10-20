"""
Unit tests for the Attach_Step (`attach_bucket`) of the shared layer
(portal-authorization-and-iam-scoping, tasks.md 2.1; design I5 "The
Attach_Step", "Client sources", "Region", "Registration-time reporting").

No AWS: the sibling's validator is a recording fake returning an
`EligibilityResult`; every S3 client comes from a recording fake installed as
`shared_utils.create_boto3_client` (credentials come from a fake
`assume_usecase_role`); audit rows land on a recorder installed as
`shared_utils.log_audit_event`; the bounded wait lands on a recorder installed
as `shared_utils.time.sleep`.

Validates: Requirements 7.6, 7.11
"""
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

import shared_utils as su

PORTAL = "111111111111"
USECASE = "222222222222"
DATA = "333333333333"

BUCKET = "attach-bucket"
DATA_BUCKET = "attach-data-bucket"
ACCESS_ROLE_ARN = f"arn:aws:iam::{USECASE}:role/DDAPortalAccessRole"
DATA_ROLE_ARN = f"arn:aws:iam::{DATA}:role/DDAPortalDataAccessRole"

MANAGED = {"dda-portal:managed": "true"}
CREATED_BY_ONLY = {"dda-portal:created-by": "portal"}


def single_account(**extra):
    record = {"usecase_id": "uc-single", "account_id": PORTAL,
              "cross_account_role_arn": f"arn:aws:iam::{PORTAL}:root", "s3_bucket": BUCKET}
    record.update(extra)
    return record


def multi_account(**extra):
    record = {"usecase_id": "uc-multi", "account_id": USECASE,
              "cross_account_role_arn": ACCESS_ROLE_ARN, "external_id": "ext-usecase",
              "s3_bucket": BUCKET}
    record.update(extra)
    return record


def with_data_account(**extra):
    return single_account(data_account_id=DATA, data_account_role_arn=DATA_ROLE_ARN,
                          data_account_external_id="ext-data", data_s3_bucket=DATA_BUCKET, **extra)


USER = {"user_id": "user-attach", "email": "user-attach@example.com", "role": "UseCaseAdmin"}
EVENT = {"httpMethod": "PUT", "requestContext": {"requestId": "r-1"}}


def client_error(code, status, operation="HeadBucket", headers=None):
    return ClientError({"Error": {"Code": code, "Message": code},
                        "ResponseMetadata": {"HTTPStatusCode": status,
                                             "HTTPHeaders": headers or {}}}, operation)


def denied(operation="ListObjectsV2"):
    return client_error("AccessDenied", 403, operation)


class FakeS3:
    """S3 client fake for one bucket. `abac_status` is the bucket's state
    (None = an empty AbacStatus); HeadBucket and ListObjectsV2 are allowed
    when ABAC is Enabled unless `reachable` forces the answer, and a denied
    HeadBucket is a 403 that still carries `x-amz-bucket-region`, as S3 sends
    it. `head_plan` / `list_plan` script the first calls (None = the default
    behaviour, an exception = raised, 'no-header' = a 200 without the
    header)."""

    def __init__(self, name, *, region="us-east-1", abac_status="Disabled", reachable=None,
                 head_plan=(), list_plan=(), abac_read_error=None, abac_write_error=None):
        self.name = name
        self.region = region
        self.abac_status = abac_status
        self.reachable = reachable
        self.head_plan = list(head_plan)
        self.list_plan = list(list_plan)
        self.abac_read_error = abac_read_error
        self.abac_write_error = abac_write_error
        self.calls = []

    def ops(self):
        return [op for op, _ in self.calls]

    def _allowed(self):
        return self.reachable if self.reachable is not None else self.abac_status == "Enabled"

    def head_bucket(self, **kwargs):
        self.calls.append(("head_bucket", kwargs))
        step = self.head_plan.pop(0) if self.head_plan else None
        if isinstance(step, BaseException):
            raise step
        if step == "no-header":
            return {"ResponseMetadata": {"HTTPStatusCode": 200, "HTTPHeaders": {}}}
        if not self._allowed():
            raise client_error("403", 403, headers={"x-amz-bucket-region": self.region})
        return {"ResponseMetadata": {"HTTPStatusCode": 200,
                                     "HTTPHeaders": {"x-amz-bucket-region": self.region}}}

    def get_bucket_abac(self, **kwargs):
        self.calls.append(("get_bucket_abac", kwargs))
        if self.abac_read_error is not None:
            raise self.abac_read_error
        if self.abac_status is None:
            return {"AbacStatus": {}}
        return {"AbacStatus": {"Status": self.abac_status}}

    def put_bucket_abac(self, **kwargs):
        self.calls.append(("put_bucket_abac", kwargs))
        if self.abac_write_error is not None:
            raise self.abac_write_error
        self.abac_status = kwargs["AbacStatus"]["Status"]

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list_objects_v2", kwargs))
        step = self.list_plan.pop(0) if self.list_plan else None
        if isinstance(step, BaseException):
            raise step
        if step is None and not self._allowed():
            raise denied()
        return {"KeyCount": 0}


class Factory:
    """Recording fake for `create_boto3_client(service, credentials, region)`.
    Returns `clients[region]` for the execution role and `clients['selector']`
    for assumed credentials."""

    def __init__(self, clients):
        self.clients = clients
        self.calls = []

    def __call__(self, service_name, credentials, region=None):
        self.calls.append((service_name, credentials, region))
        assert service_name == "s3"
        if not credentials.get("is_default_credentials"):
            return self.clients["selector"]
        return self.clients.get(region) or self.clients[None]


class Recorder:
    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result
        self.raises = raises

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.result


def eligibility(tags):
    return su.EligibilityResult(eligible=True, reason="eligible", tags=dict(tags),
                                portal_created=tags.get("dda-portal:created-by") == "portal",
                                claimed_by=None)


@pytest.fixture
def env(monkeypatch):
    """Installs the recording seams; `env.install(...)` sets the clients and
    the validator's result."""
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL)
    monkeypatch.delenv("DATA_PLANE_SCOPING", raising=False)
    audit = Recorder()
    sleeps = Recorder()
    assumes = []

    def fake_assume(role_arn, external_id, session_name):
        assumes.append((role_arn, external_id, session_name))
        if role_arn is None:
            raise ValueError("no role to assume")
        return {"AccessKeyId": "test", "SecretAccessKey": "test", "SessionToken": "test",
                "role_arn": role_arn, "session_name": session_name}

    monkeypatch.setattr(su, "log_audit_event", audit)
    monkeypatch.setattr(su.time, "sleep", sleeps)
    monkeypatch.setattr(su, "assume_usecase_role", fake_assume)
    state = SimpleNamespace(audit=audit, sleeps=sleeps, assumes=assumes, monkeypatch=monkeypatch)

    def install(clients, tags=MANAGED, validator_raises=None):
        factory = Factory(clients)
        validator = Recorder(result=eligibility(tags), raises=validator_raises)
        monkeypatch.setattr(su, "create_boto3_client", factory)
        monkeypatch.setattr(su, "check_bucket_attachable", validator)
        state.factory, state.validator = factory, validator
        return state

    state.install = install
    return state


def attach(usecase=None, bucket=BUCKET, **overrides):
    kwargs = dict(target_usecase_id="uc-single", usecase_like=usecase or single_account(),
                  bucket=bucket, user=USER, event=EVENT, purpose="s3_bucket", source="api")
    kwargs.update(overrides)
    return su.attach_bucket(**kwargs)


def abac_rows(audit):
    return [(args, kwargs) for args, kwargs in audit.calls if args[1] == "bucket_abac_enabled"]


# ---------------------------------------------------------------------------
# constants and shapes
# ---------------------------------------------------------------------------

def test_constants():
    assert su.ATTACH_SESSION_NAME == "portal-bucket-attach"
    assert su.ATTACH_SESSION_NAME != su.ELIGIBILITY_SESSION_NAME
    assert su.ATTACH_PROBE_RETRY_DELAY_SECONDS == 1


def test_report_carries_exactly_the_four_fields():
    outcome = su.AttachOutcome(eligibility(MANAGED), True, True, False, "r", "enabled")
    assert outcome.report() == {"tagged": True, "abac_enabled": True,
                                "iam_reachable": False, "remedy": "r"}


# ---------------------------------------------------------------------------
# the validator: every keyword forwarded; a refusal propagates untouched
# ---------------------------------------------------------------------------

def test_every_keyword_is_forwarded_to_the_validator(env):
    env.install({None: FakeS3("default", abac_status="Enabled")})
    evidence = eligibility(MANAGED)
    usecase = single_account()
    su.attach_bucket(target_usecase_id="uc-9", usecase_like=usecase, bucket=BUCKET, user=USER,
                     event=EVENT, purpose="additional_s3_buckets", evidence=evidence, retry=True,
                     source="create")
    assert env.validator.calls == [((), {
        "target_usecase_id": "uc-9", "usecase_like": usecase, "bucket": BUCKET, "user": USER,
        "event": EVENT, "purpose": "additional_s3_buckets", "evidence": evidence, "retry": True})]


def test_defaults_are_forwarded(env):
    env.install({None: FakeS3("default", abac_status="Enabled")})
    attach()
    (_, kwargs), = env.validator.calls
    assert kwargs["evidence"] is None and kwargs["retry"] is False
    assert "source" not in kwargs


def test_refusal_propagates_and_nothing_else_runs(env):
    refusal = su.BucketNotEligible("S3 bucket is not eligible for this use case",
                                   bucket=BUCKET, reason="tag_missing")
    env.install({None: FakeS3("default")}, validator_raises=refusal)
    with pytest.raises(su.BucketNotEligible) as info:
        attach()
    assert info.value is refusal
    assert env.factory.calls == [] and env.assumes == []
    assert env.audit.calls == [] and env.sleeps.calls == []


# ---------------------------------------------------------------------------
# Portal_Account_Bucket: ABAC enable, already enabled, tag missing
# ---------------------------------------------------------------------------

def test_abac_off_bucket_is_enabled_once_and_audited(env):
    s3 = FakeS3("bucket")                       # one fake answers every region
    env.install({None: s3})
    outcome = attach()
    # region resolver, status read, enable, then the probe
    assert s3.ops() == ["head_bucket", "get_bucket_abac", "put_bucket_abac",
                        "head_bucket", "list_objects_v2"]
    assert s3.calls[2][1] == {"Bucket": BUCKET, "AbacStatus": {"Status": "Enabled"}}
    assert s3.calls[4][1] == {"Bucket": BUCKET, "MaxKeys": 1}
    assert [region for _, _, region in env.factory.calls] == [None, "us-east-1", None]
    assert (outcome.tagged, outcome.abac_enabled, outcome.abac_action) == (True, True, "enabled")
    assert (outcome.iam_reachable, outcome.remedy) == (True, None)
    assert env.audit.calls == [((USER["user_id"], "bucket_abac_enabled", "usecase", "uc-single",
                                 "success", {"bucket": BUCKET, "source": "api"}),
                                {"event": EVENT})]
    assert env.sleeps.calls == []


def test_abac_on_bucket_is_left_alone(env):
    default = FakeS3("default", abac_status="Enabled")
    env.install({None: default})
    outcome = attach()
    assert "put_bucket_abac" not in default.ops()
    assert default.ops() == ["head_bucket", "get_bucket_abac", "head_bucket", "list_objects_v2"]
    assert (outcome.abac_enabled, outcome.abac_action) == (True, "already_enabled")
    assert (outcome.iam_reachable, outcome.remedy) == (True, None)
    assert env.audit.calls == [] and env.sleeps.calls == []


def test_empty_abac_status_reads_as_not_enabled(env):
    default = FakeS3("default", abac_status=None)
    env.install({None: default})
    outcome = attach()
    assert "put_bucket_abac" in default.ops()
    assert outcome.abac_action == "enabled"


def test_created_by_only_bucket_gets_no_abac_call_and_the_tag_remedy(env):
    default = FakeS3("default")
    env.install({None: default}, tags=CREATED_BY_ONLY)
    outcome = attach()
    assert default.ops() == ["head_bucket"]      # the probe only; denied at HeadBucket
    assert outcome.eligibility.eligible is True
    assert (outcome.tagged, outcome.abac_enabled, outcome.abac_action) == (False, False, "tag_missing")
    assert outcome.iam_reachable is False
    assert outcome.remedy == su.REGISTRATION_REMEDY_TAG.format(account=PORTAL, bucket=BUCKET)
    assert "aws s3control tag-resource --account-id " + PORTAL in outcome.remedy
    assert env.audit.calls == []


def test_untagged_but_reachable_bucket_has_no_remedy(env):
    env.install({None: FakeS3("default", reachable=True)}, tags=CREATED_BY_ONLY)
    outcome = attach()
    assert (outcome.tagged, outcome.abac_action, outcome.iam_reachable, outcome.remedy) == (
        False, "tag_missing", True, None)


# ---------------------------------------------------------------------------
# buckets that are not Portal_Account_Buckets
# ---------------------------------------------------------------------------

def test_separate_data_account_bucket_is_not_enabled(env):
    selector = FakeS3("selector", reachable=True)
    default = FakeS3("default")
    env.install({None: default, "selector": selector})
    outcome = attach(usecase=with_data_account(), bucket=DATA_BUCKET, purpose="data_s3_bucket")
    assert env.assumes == [(DATA_ROLE_ARN, "ext-data", su.ATTACH_SESSION_NAME)]
    assert selector.ops() == ["head_bucket", "list_objects_v2"]
    assert default.calls == []
    assert (outcome.abac_enabled, outcome.abac_action) == ("unknown", "not_applicable")
    assert (outcome.iam_reachable, outcome.remedy) == (True, None)
    assert env.audit.calls == []


def test_multi_account_bucket_is_not_enabled_and_names_the_administrator(env):
    selector = FakeS3("selector", reachable=False)
    env.install({None: FakeS3("default"), "selector": selector})
    outcome = attach(usecase=multi_account(), target_usecase_id="uc-multi")
    assert env.assumes == [(ACCESS_ROLE_ARN, "ext-usecase", su.ATTACH_SESSION_NAME)]
    (service, credentials, region), = env.factory.calls
    assert (service, region, credentials["session_name"]) == ("s3", None, su.ATTACH_SESSION_NAME)
    assert "get_bucket_abac" not in selector.ops() and "put_bucket_abac" not in selector.ops()
    assert (outcome.tagged, outcome.abac_enabled, outcome.abac_action) == (True, "unknown", "not_applicable")
    assert outcome.iam_reachable is False
    assert outcome.remedy == su.REGISTRATION_REMEDY_CROSS_ACCOUNT.format(bucket=BUCKET)


def test_multi_account_untagged_bucket_tag_remedy_names_the_bucket_account(env):
    env.install({None: FakeS3("default"), "selector": FakeS3("selector", reachable=False)},
                tags=CREATED_BY_ONLY)
    outcome = attach(usecase=multi_account())
    assert outcome.remedy == su.REGISTRATION_REMEDY_TAG.format(account=USECASE, bucket=BUCKET)


def test_none_arn_record_is_not_a_portal_account_bucket(env):
    legacy = {"usecase_id": "uc-legacy", "account_id": PORTAL, "s3_bucket": BUCKET}
    assert su._is_portal_account_bucket(legacy, BUCKET) is False
    assert su._is_portal_account_bucket(single_account(), BUCKET) is True
    default = FakeS3("default")
    env.install({None: default, "selector": FakeS3("selector")})
    outcome = attach(usecase=legacy)
    assert default.calls == []
    assert (outcome.abac_enabled, outcome.abac_action, outcome.iam_reachable) == (
        "unknown", "not_applicable", False)
    assert outcome.remedy is not None
    assert env.audit.calls == []


# ---------------------------------------------------------------------------
# one failing call each: the Attach returns normally with the stated fields
# ---------------------------------------------------------------------------

def assert_enable_failed(env, outcome, reason_prefix):
    assert (outcome.abac_enabled, outcome.abac_action) == (False, "enable_failed")
    (args, kwargs), = abac_rows(env.audit)
    assert args[4] == "failure"
    assert args[5]["bucket"] == BUCKET and args[5]["source"] == "api"
    assert args[5]["reason"].startswith(reason_prefix)
    assert len(args[5]["reason"]) <= 200
    assert kwargs == {"event": EVENT}
    assert outcome.iam_reachable is False
    assert outcome.remedy == su.REGISTRATION_REMEDY_ENABLE_ABAC.format(bucket=BUCKET)


def test_region_unknown_is_enable_failed(env):
    default = FakeS3("default", head_plan=[client_error("403", 403)])   # 403 without the header
    env.install({None: default})
    outcome = attach()
    assert "get_bucket_abac" not in default.ops() and "put_bucket_abac" not in default.ops()
    assert_enable_failed(env, outcome, "RegionUnknown")


def test_region_header_absent_on_a_200_is_enable_failed(env):
    default = FakeS3("default", head_plan=["no-header"])
    env.install({None: default})
    outcome = attach()
    assert_enable_failed(env, outcome, "RegionUnknown")


def test_get_bucket_abac_error_is_enable_failed(env):
    default = FakeS3("default", abac_read_error=client_error("AccessDenied", 403, "GetBucketAbac"))
    env.install({None: default})
    outcome = attach()
    assert "put_bucket_abac" not in default.ops()
    assert_enable_failed(env, outcome, "ClientError")


def test_put_bucket_abac_error_is_enable_failed(env):
    default = FakeS3("default", abac_write_error=client_error("AccessDenied", 403, "PutBucketAbac"))
    env.install({None: default})
    outcome = attach()
    assert default.ops().count("put_bucket_abac") == 1
    assert_enable_failed(env, outcome, "ClientError")
    assert env.sleeps.calls == []


def test_head_bucket_connection_error_in_the_probe(env):
    connection_error = EndpointConnectionError(endpoint_url="fake-endpoint")
    default = FakeS3("default", abac_status="Enabled", head_plan=[None, connection_error])
    env.install({None: default})
    outcome = attach()
    assert default.ops() == ["head_bucket", "get_bucket_abac", "head_bucket"]
    assert (outcome.abac_action, outcome.iam_reachable) == ("already_enabled", False)
    assert outcome.remedy == su.REGISTRATION_REMEDY_ENABLE_ABAC.format(bucket=BUCKET)


def test_head_bucket_connection_error_in_the_region_resolver(env):
    connection_error = EndpointConnectionError(endpoint_url="fake-endpoint")
    default = FakeS3("default", head_plan=[connection_error])
    env.install({None: default})
    outcome = attach()
    assert_enable_failed(env, outcome, "EndpointConnectionError")


def test_list_objects_error_in_the_probe(env):
    default = FakeS3("default", abac_status="Enabled",
                     list_plan=[client_error("AccessDenied", 403, "ListObjectsV2")])
    env.install({None: default})
    outcome = attach()
    assert default.ops()[-2:] == ["head_bucket", "list_objects_v2"]
    assert (outcome.abac_action, outcome.iam_reachable) == ("already_enabled", False)
    assert outcome.remedy is not None


def test_unexpected_exception_from_put_bucket_abac_is_caught(env):
    default = FakeS3("default", abac_write_error=RuntimeError("x" * 500))
    env.install({None: default})
    outcome = attach()
    assert_enable_failed(env, outcome, "RuntimeError: xxx")


def test_execution_role_client_failure_is_reported_not_raised(env):
    def broken_factory(service_name, credentials, region=None):
        raise RuntimeError("no client")

    env.install({None: FakeS3("default")})
    env.monkeypatch.setattr(su, "create_boto3_client", broken_factory)
    outcome = attach()
    assert (outcome.abac_action, outcome.iam_reachable) == ("enable_failed", False)
    assert outcome.remedy == su.REGISTRATION_REMEDY_ENABLE_ABAC.format(bucket=BUCKET)


# ---------------------------------------------------------------------------
# region resolution: from a 200 and from an error response's headers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("head_step", [
    None,                                                                      # 200 with the header
    client_error("403", 403, headers={"x-amz-bucket-region": "eu-west-1"}),
    client_error("301", 301, headers={"x-amz-bucket-region": "eu-west-1"}),
])
def test_abac_calls_go_to_the_bucket_region(env, head_step):
    default = FakeS3("default", region="eu-west-1", reachable=True,
                     head_plan=[head_step] if head_step is not None else [])
    regional = FakeS3("regional", region="eu-west-1")
    env.install({None: default, "eu-west-1": regional})
    outcome = attach()
    assert [region for _, _, region in env.factory.calls] == [None, "eu-west-1", None]
    assert regional.ops() == ["get_bucket_abac", "put_bucket_abac"]
    assert "get_bucket_abac" not in default.ops()
    assert outcome.abac_action == "enabled"


def test_region_client_cache_builds_one_client_per_region(env):
    default = FakeS3("default", region="eu-west-1")
    env.install({None: default, "eu-west-1": FakeS3("regional")})
    cache = {}
    first = su._s3_client_for_bucket_region(BUCKET, cache)
    second = su._s3_client_for_bucket_region(BUCKET, cache)
    assert first is second
    assert [region for _, _, region in env.factory.calls] == [None, "eu-west-1"]


def test_region_resolver_raises_region_unknown_without_the_header(env):
    env.install({None: FakeS3("default", head_plan=[client_error("NoSuchBucket", 404)])})
    with pytest.raises(su.RegionUnknown):
        su._s3_client_for_bucket_region(BUCKET, {})


# ---------------------------------------------------------------------------
# the bounded wait after a just-enabled bucket
# ---------------------------------------------------------------------------

def test_one_retry_after_enable_denied_once_then_allowed(env):
    default = FakeS3("default", reachable=True, list_plan=[denied()])
    env.install({None: default})
    outcome = attach()
    assert outcome.abac_action == "enabled"
    assert env.sleeps.calls == [((su.ATTACH_PROBE_RETRY_DELAY_SECONDS,), {})]
    assert default.ops()[-4:] == ["head_bucket", "list_objects_v2", "head_bucket", "list_objects_v2"]
    assert (outcome.iam_reachable, outcome.remedy) == (True, None)


def test_one_retry_after_enable_denied_twice(env):
    default = FakeS3("default", reachable=False)
    env.install({None: default})
    outcome = attach()
    assert outcome.abac_action == "enabled"
    assert env.sleeps.calls == [((su.ATTACH_PROBE_RETRY_DELAY_SECONDS,), {})]
    assert outcome.iam_reachable is False
    assert outcome.remedy == su.REGISTRATION_REMEDY_ENABLE_ABAC.format(bucket=BUCKET)


def test_no_wait_unless_the_step_just_enabled(env):
    env.install({None: FakeS3("default", abac_status="Enabled", reachable=False)})
    outcome = attach()
    assert outcome.abac_action == "already_enabled" and outcome.iam_reachable is False
    assert env.sleeps.calls == []
    env.install({None: FakeS3("default", reachable=False)}, tags=CREATED_BY_ONLY)
    attach()
    assert env.sleeps.calls == []


# ---------------------------------------------------------------------------
# no Attach_Step client escapes create_boto3_client
# ---------------------------------------------------------------------------

def test_no_attach_step_client_escapes_the_factory(env):
    escaped = Recorder(raises=AssertionError("boto3.client called directly"))
    env.monkeypatch.setattr(su, "boto3", SimpleNamespace(client=escaped, resource=su.boto3.resource))

    default = FakeS3("default", reachable=True)
    env.install({None: default, "us-east-1": default, "selector": FakeS3("selector", reachable=True)})
    attach()                                          # Portal_Account_Bucket, ABAC enabled here
    attach(usecase=multi_account())                   # assumed role
    attach(usecase=with_data_account(), bucket=DATA_BUCKET)
    assert escaped.calls == []
    calls = env.factory.calls
    assert calls[:3] == [("s3", {"is_default_credentials": True}, None),
                         ("s3", {"is_default_credentials": True}, "us-east-1"),
                         ("s3", {"is_default_credentials": True}, None)]
    assumed = [credentials for _, credentials, _ in calls[3:]]
    assert [c["session_name"] for c in assumed] == [su.ATTACH_SESSION_NAME] * 2
    assert [c["role_arn"] for c in assumed] == [ACCESS_ROLE_ARN, DATA_ROLE_ARN]


# ---------------------------------------------------------------------------
# remedies (Requirement 7.11) and the scoping accessor
# ---------------------------------------------------------------------------

def test_remedy_is_none_iff_reachable():
    for tagged in (True, False):
        for kind in ("portal", "cross_account"):
            assert su.registration_remedy(tagged, False, True, kind, "tag", bucket=BUCKET) is None
            assert su.registration_remedy(tagged, False, False, kind, "tag", bucket=BUCKET)


def test_remedy_tag_first(monkeypatch):
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL)
    remedy = su.registration_remedy(False, True, False, "portal", "tag", bucket=BUCKET)
    assert remedy == (
        "Tag the bucket dda-portal:managed=true: `aws s3control tag-resource "
        f"--account-id {PORTAL} --resource-arn arn:aws:s3:::{BUCKET} "
        "--tags Key=dda-portal:managed,Value=true`")
    explicit = su.registration_remedy(False, "unknown", False, "cross_account", "tag",
                                      bucket=BUCKET, account_id=USECASE)
    assert f"--account-id {USECASE} " in explicit
    monkeypatch.delenv("PORTAL_ACCOUNT_ID")
    placeholder = su.registration_remedy(False, False, False, "portal", "tag", bucket=BUCKET)
    assert "--account-id <bucket owner account> " in placeholder
    unknown = su.registration_remedy(False, "unknown", False, "cross_account", "tag", bucket=BUCKET)
    assert "--account-id <bucket owner account> " in unknown


def test_remedy_enable_abac_for_a_portal_bucket():
    remedy = su.registration_remedy(True, False, False, "portal", "tag", bucket=BUCKET)
    assert remedy == ("Enable ABAC on the bucket: `aws s3api put-bucket-abac --bucket "
                      f"{BUCKET} --abac-status Status=Enabled`")


def test_remedy_cross_account_asks_the_account_administrator():
    remedy = su.registration_remedy(True, "unknown", False, "cross_account", "tag", bucket=BUCKET)
    assert remedy.startswith("Ask the bucket's account administrator to enable ABAC")
    assert f"put-bucket-abac --bucket {BUCKET} --abac-status Status=Enabled" in remedy


def test_remedy_redeploy_only_under_tag_objects_only():
    assert su.registration_remedy(True, True, False, "portal", "tag-objects-only",
                                  bucket=BUCKET) == su.REGISTRATION_REMEDY_REDEPLOY
    assert su.registration_remedy(True, True, False, "portal", "tag",
                                  bucket=BUCKET) == su.REGISTRATION_REMEDY_ENABLE_ABAC.format(bucket=BUCKET)
    # an unmet earlier step still wins under tag-objects-only
    assert su.registration_remedy(True, False, False, "portal", "tag-objects-only",
                                  bucket=BUCKET) == su.REGISTRATION_REMEDY_ENABLE_ABAC.format(bucket=BUCKET)


@pytest.mark.parametrize("scoping, expected", [
    ("tag-objects-only", su.REGISTRATION_REMEDY_REDEPLOY),
    ("tag", su.REGISTRATION_REMEDY_ENABLE_ABAC.format(bucket=BUCKET)),
])
def test_attach_reads_the_scoping_from_the_environment(env, scoping, expected):
    env.monkeypatch.setenv("DATA_PLANE_SCOPING", scoping)
    default = FakeS3("default", abac_status="Enabled", list_plan=[denied()])
    env.install({None: default})
    outcome = attach()
    assert (outcome.abac_enabled, outcome.iam_reachable) == (True, False)
    assert outcome.remedy == expected


# ---------------------------------------------------------------------------
# _abac_status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("response, expected", [
    ({"AbacStatus": {"Status": "Enabled"}}, "Enabled"),
    ({"AbacStatus": {"Status": "Disabled"}}, "Disabled"),
    ({"AbacStatus": {}}, "Disabled"),                      # moto's answer
    ({"AbacStatus": None}, "Disabled"),
    ({}, "Disabled"),
    ({"AbacStatus": {"Status": "enabled"}}, "Disabled"),
])
def test_abac_status(response, expected):
    client = SimpleNamespace(get_bucket_abac=lambda **kwargs: response)
    assert su._abac_status(client, BUCKET) == expected


def test_abac_status_errors_reach_the_caller():
    error = client_error("NoSuchBucket", 404, "GetBucketAbac")

    def get_bucket_abac(**kwargs):
        raise error

    with pytest.raises(ClientError):
        su._abac_status(SimpleNamespace(get_bucket_abac=get_bucket_abac), BUCKET)
