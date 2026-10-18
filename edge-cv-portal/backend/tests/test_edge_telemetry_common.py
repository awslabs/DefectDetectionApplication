"""The shared Portal package ``edge_telemetry_common``
(workflow-debug-mode tasks.md 3.2; workflow-telemetry-dashboards design §2.3,
§10.2, §14.4, §6.4).

- ``topology``: both predicates, including a ``:role/`` ARN in the Portal
  account (``portal``) and the missing-environment case;
- ``credentials.client``: the two branches, the cache key, the refresh under
  20 minutes remaining, the ``Policy`` text STS receives, the unknown purpose;
- ``session_policies``: every purpose's statements, sizes with the longest
  arguments, the unknown purpose and the missing-argument errors;
- ``devices.usecase_devices``: a moto Devices table, pagination through an
  injected paging table, the 60 s cache;
- ``identity``: error classification, the suffix strip, every answer of
  ``resolve_thing`` and its caches;
- ``shadow_pool.write_all``: the ceiling and the result shape.
"""
import json
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError

REGION = "us-east-1"
PORTAL_ACCOUNT = "111111111111"
CROSS_ACCOUNT = "222222222222"
CROSS_ROLE = "arn:aws:iam::{0}:role/DDAPortalAccessRole".format(CROSS_ACCOUNT)
UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"


def _reimport(name):
    for key in list(sys.modules):
        if key == name or key.startswith(name + "."):
            sys.modules.pop(key, None)
    return __import__(name)


@pytest.fixture
def common(aws_stack, monkeypatch):
    """The package re-imported inside the moto stack with the Portal account
    set, so ``shared_utils`` is the real layer the stack imported."""
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL_ACCOUNT)
    package = _reimport("edge_telemetry_common")
    from edge_telemetry_common import credentials, devices, identity, session_policies, shadow_pool, topology
    credentials.clear_cache()
    devices.clear_cache()
    identity.clear_cache()
    yield package
    credentials.clear_cache()
    devices.clear_cache()
    identity.clear_cache()


def portal_usecase(**extra):
    usecase = {"usecase_id": UC, "account_id": PORTAL_ACCOUNT, "region": REGION,
               "cross_account_role_arn": "arn:aws:iam::{0}:role/DDAPortalAccessRole".format(PORTAL_ACCOUNT),
               "external_id": "fixture-eid"}
    usecase.update(extra)
    return usecase


def cross_usecase(**extra):
    usecase = {"usecase_id": UC, "account_id": CROSS_ACCOUNT, "region": REGION,
               "cross_account_role_arn": CROSS_ROLE, "external_id": "fixture-eid"}
    usecase.update(extra)
    return usecase


# --------------------------------------------------------------------------- topology
class TestTopology:
    def test_portal_account_is_portal_even_with_a_role_arn(self, common):
        from edge_telemetry_common.topology import telemetry_topology
        assert telemetry_topology(portal_usecase()) == "portal"
        assert telemetry_topology(portal_usecase(cross_account_role_arn="arn:aws:iam::{0}:root".format(PORTAL_ACCOUNT))) == "portal"

    def test_other_account_is_cross(self, common):
        from edge_telemetry_common.topology import telemetry_topology
        assert telemetry_topology(cross_usecase()) == "cross"
        assert telemetry_topology({"usecase_id": UC}) == "cross"
        assert telemetry_topology(cross_usecase(account_id=int(CROSS_ACCOUNT))) == "cross"

    def test_missing_portal_account_environment_raises(self, common, monkeypatch):
        from edge_telemetry_common.topology import telemetry_topology
        monkeypatch.delenv("PORTAL_ACCOUNT_ID")
        with pytest.raises(KeyError):
            telemetry_topology(portal_usecase())


# --------------------------------------------------------------------------- session policies
class TestSessionPolicies:
    LONG_BUCKET = "b" + "x" * 61 + "b"
    LONG_USECASE = "u" * 64

    def test_every_purpose_is_under_a_thousand_characters_with_the_longest_arguments(self, common):
        from edge_telemetry_common import session_policies as sp
        for purpose in sp.PURPOSES:
            policy = sp.build(purpose, usecase={"usecase_id": self.LONG_USECASE}, bucket=self.LONG_BUCKET)
            assert len(sp.policy_text(policy)) < 1000, purpose
            assert len(json.dumps(policy)) < 1000, purpose
            assert policy["Version"] == "2012-10-17"

    def test_ingest(self, common):
        from edge_telemetry_common import session_policies as sp
        assert sp.build("ingest")["Statement"] == [{
            "Effect": "Allow", "Action": "iot:ListPrincipalThings", "Resource": "arn:aws:iot:*:*:cert/*"}]

    def test_media_retention_read(self, common):
        from edge_telemetry_common import session_policies as sp
        assert sp.build("media-retention-read", bucket="dda-inference-results-222222222222")["Statement"] == [{
            "Effect": "Allow",
            "Action": ["s3:GetLifecycleConfiguration", "s3:GetBucketVersioning"],
            "Resource": "arn:aws:s3:::dda-inference-results-222222222222"}]

    def test_debug_control(self, common):
        from edge_telemetry_common import session_policies as sp
        statements = sp.build("debug-control")["Statement"]
        assert statements == [
            {"Effect": "Allow", "Action": ["iot:UpdateThingShadow", "iot:GetThingShadow"],
             "Resource": "arn:aws:iot:*:*:thing/*"},
            {"Effect": "Allow", "Action": "iot:GetTopicRule", "Resource": "arn:aws:iot:*:*:rule/dda_dbg_ingest"},
        ]

    def test_image_read(self, common):
        from edge_telemetry_common import session_policies as sp
        statements = sp.build("image-read", usecase={"usecase_id": UC}, bucket="results-bucket")["Statement"]
        assert statements == [
            {"Effect": "Allow", "Action": "s3:GetObject",
             "Resource": "arn:aws:s3:::results-bucket/dda-telemetry/debug/{0}/*".format(UC)},
            {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::results-bucket",
             "Condition": {"StringLike": {"s3:prefix": "dda-telemetry/debug/{0}/*".format(UC)}}},
        ]
        # The use case may be given as its id.
        assert sp.build("image-read", usecase=UC, bucket="results-bucket")["Statement"] == statements

    # The dashboards' three purposes (workflow-telemetry-dashboards tasks.md 2.2, design §14.4).
    def test_api_control(self, common):
        from edge_telemetry_common import session_policies as sp
        assert sp.PURPOSES == ("ingest", "media-retention-read", "debug-control", "image-read",
                               "api-control", "media-read", "live-expiry")
        statements = sp.build("api-control")["Statement"]
        assert statements == [
            {"Effect": "Allow", "Action": ["iot:UpdateThingShadow", "iot:GetThingShadow"],
             "Resource": "arn:aws:iot:*:*:thing/*"},
            {"Effect": "Allow",
             "Action": ["greengrass:GetCoreDevice", "greengrass:ListCoreDevices", "greengrass:ListTagsForResource"],
             "Resource": "*"},
            {"Effect": "Allow", "Action": "iot:GetTopicRule",
             "Resource": ["arn:aws:iot:*:*:rule/dda_tel_ingest", "arn:aws:iot:*:*:rule/dda_dbg_ingest"]},
        ]
        # No argument is needed; given ones are ignored.
        assert sp.build("api-control", usecase={"usecase_id": UC}, bucket="results-bucket")["Statement"] == statements

    def test_media_read(self, common):
        from edge_telemetry_common import session_policies as sp
        statements = sp.build("media-read", usecase={"usecase_id": UC}, bucket="results-bucket")["Statement"]
        assert statements == [
            {"Effect": "Allow", "Action": "s3:GetObject",
             "Resource": "arn:aws:s3:::results-bucket/dda-telemetry/*/{0}/*".format(UC)},
            {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::results-bucket",
             "Condition": {"StringLike": {"s3:prefix": "dda-telemetry/*/{0}/*".format(UC)}}},
        ]
        assert sp.build("media-read", usecase=UC, bucket="results-bucket")["Statement"] == statements
        # No write action, nothing under another use case's prefix.
        text = sp.policy_text(sp.build("media-read", usecase=UC, bucket="results-bucket"))
        assert "Put" not in text and "Delete" not in text

    def test_live_expiry(self, common):
        from edge_telemetry_common import session_policies as sp
        statements = sp.build("live-expiry", usecase={"usecase_id": UC}, bucket="results-bucket")["Statement"]
        assert statements == [
            {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::results-bucket",
             "Condition": {"StringLike": {"s3:prefix": "dda-telemetry/live/{0}/*".format(UC)}}},
            {"Effect": "Allow", "Action": "s3:DeleteObject",
             "Resource": "arn:aws:s3:::results-bucket/dda-telemetry/live/{0}/*".format(UC)},
        ]
        text = sp.policy_text(sp.build("live-expiry", usecase=UC, bucket="results-bucket"))
        assert "GetObject" not in text and "clips" not in text and "debug" not in text

    def test_the_three_new_policies_stay_under_a_thousand_characters_with_the_longest_arguments(self, common):
        from edge_telemetry_common import session_policies as sp
        for purpose in ("api-control", "media-read", "live-expiry"):
            policy = sp.build(purpose, usecase={"usecase_id": self.LONG_USECASE}, bucket=self.LONG_BUCKET)
            assert len(sp.policy_text(policy)) < 1000, purpose

    def test_unknown_purpose_raises(self, common):
        from edge_telemetry_common import session_policies as sp
        with pytest.raises(sp.UnknownPurpose):
            sp.build("clip-write")
        with pytest.raises(ValueError):
            sp.build("nope")

    @pytest.mark.parametrize("purpose", ["media-retention-read", "image-read", "media-read", "live-expiry"])
    def test_missing_or_malformed_bucket_raises(self, common, purpose):
        from edge_telemetry_common import session_policies as sp
        with pytest.raises(ValueError):
            sp.build(purpose, usecase={"usecase_id": UC})
        with pytest.raises(ValueError):
            sp.build(purpose, usecase={"usecase_id": UC}, bucket="Bad*Name")

    @pytest.mark.parametrize("purpose", ["image-read", "media-read", "live-expiry"])
    def test_prefixed_purposes_need_a_valid_usecase_id(self, common, purpose):
        from edge_telemetry_common import session_policies as sp
        with pytest.raises(ValueError):
            sp.build(purpose, bucket="results-bucket")
        with pytest.raises(ValueError):
            sp.build(purpose, usecase={"usecase_id": "a/*"}, bucket="results-bucket")


# --------------------------------------------------------------------------- credentials
class _RecordingSts:
    def __init__(self, clock):
        self.calls = []
        self.clock = clock

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        n = len(self.calls)
        return {"Credentials": {
            "AccessKeyId": "ASIAFIXTUREKEYID{0:04d}".format(n),
            "SecretAccessKey": "fixture-secret-{0}".format(n),
            "SessionToken": "fixture-token-{0}".format(n),
            "Expiration": datetime.fromtimestamp(self.clock(), tz=timezone.utc) + timedelta(hours=1),
        }}


class _FakeClock:
    def __init__(self, start=1_700_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def stubbed_clients(common, monkeypatch):
    """``boto3.client`` recorded in both the shared layer and the package; a
    fake clock drives the session cache."""
    import shared_utils
    from edge_telemetry_common import credentials
    clock = _FakeClock()
    sts = _RecordingSts(clock)
    created = []

    def fake_client(service, *args, **kwargs):
        created.append((service, kwargs))
        if service == "sts":
            return sts
        return ("client", service, kwargs)

    monkeypatch.setattr(shared_utils.boto3, "client", fake_client)
    monkeypatch.setattr(credentials.boto3, "client", fake_client)
    monkeypatch.setattr(credentials, "_session_cache", credentials.SessionCache(clock=clock))
    return type("Stub", (), {"sts": sts, "created": created, "clock": clock})()


class TestCredentialsClient:
    def test_portal_topology_uses_the_functions_own_role(self, stubbed_clients):
        from edge_telemetry_common import credentials
        client = credentials.client("iot", portal_usecase(), "ingest")
        assert client == ("client", "iot", {"region_name": REGION})
        assert stubbed_clients.sts.calls == []
        assert all(kwargs.get("aws_access_key_id") is None for _, kwargs in stubbed_clients.created)

    def test_portal_topology_region_override_and_default(self, stubbed_clients, monkeypatch):
        from edge_telemetry_common import credentials
        assert credentials.client("s3", portal_usecase(), "image-read", region="eu-west-1")[2] == {"region_name": "eu-west-1"}
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        assert credentials.client("s3", portal_usecase(region=None), "image-read")[2] == {"region_name": "us-west-2"}

    def test_cross_topology_assumes_the_role_with_the_purpose_policy(self, stubbed_clients):
        from edge_telemetry_common import credentials, session_policies as sp
        client = credentials.client("iot", cross_usecase(), "ingest")
        assert len(stubbed_clients.sts.calls) == 1
        call = stubbed_clients.sts.calls[0]
        assert call["RoleArn"] == CROSS_ROLE
        assert call["ExternalId"] == "fixture-eid"
        assert call["RoleSessionName"] == "dda-telemetry-ingest"
        assert json.loads(call["Policy"]) == sp.build("ingest")
        assert call["Policy"] == json.dumps(sp.build("ingest"))
        assert client[1] == "iot"
        assert client[2]["aws_access_key_id"] == "ASIAFIXTUREKEYID0001"
        assert client[2]["aws_session_token"] == "fixture-token-1"
        assert client[2]["region_name"] == REGION

    def test_cache_is_keyed_by_role_purpose_and_policy_text(self, stubbed_clients):
        from edge_telemetry_common import credentials
        credentials.client("iot", cross_usecase(), "ingest")
        credentials.client("iot", cross_usecase(), "ingest")
        assert len(stubbed_clients.sts.calls) == 1
        credentials.client("iot-data", cross_usecase(), "debug-control")
        assert len(stubbed_clients.sts.calls) == 2
        # Same purpose, different policy text (another bucket): another session.
        credentials.client("s3", cross_usecase(), "image-read", bucket="bucket-a")
        credentials.client("s3", cross_usecase(), "image-read", bucket="bucket-b")
        credentials.client("s3", cross_usecase(), "image-read", bucket="bucket-a")
        assert len(stubbed_clients.sts.calls) == 4
        assert "bucket-a" in stubbed_clients.sts.calls[2]["Policy"]
        assert "bucket-b" in stubbed_clients.sts.calls[3]["Policy"]
        # Another role ARN: another session.
        other = cross_usecase(account_id="333333333333",
                              cross_account_role_arn="arn:aws:iam::333333333333:role/DDAPortalAccessRole")
        credentials.client("iot", other, "ingest")
        assert len(stubbed_clients.sts.calls) == 5

    def test_session_is_refreshed_under_twenty_minutes_remaining(self, stubbed_clients):
        from edge_telemetry_common import credentials
        credentials.client("iot", cross_usecase(), "ingest")
        stubbed_clients.clock.advance(39 * 60)
        credentials.client("iot", cross_usecase(), "ingest")
        assert len(stubbed_clients.sts.calls) == 1, "more than 20 minutes remain: no refresh"
        stubbed_clients.clock.advance(2 * 60)
        client = credentials.client("iot", cross_usecase(), "ingest")
        assert len(stubbed_clients.sts.calls) == 2, "fewer than 20 minutes remain: refreshed"
        assert client[2]["aws_access_key_id"] == "ASIAFIXTUREKEYID0002"

    def test_unknown_purpose_raises_in_both_topologies(self, stubbed_clients):
        from edge_telemetry_common import credentials, session_policies as sp
        with pytest.raises(sp.UnknownPurpose):
            credentials.client("iot", cross_usecase(), "clip-write")
        with pytest.raises(sp.UnknownPurpose):
            credentials.client("iot", portal_usecase(), "clip-write")
        assert stubbed_clients.sts.calls == []

    def test_the_dashboards_purposes_assume_the_role_with_their_policies(self, stubbed_clients):
        from edge_telemetry_common import credentials, session_policies as sp
        credentials.client("iot-data", cross_usecase(), "api-control")
        credentials.client("s3", cross_usecase(), "media-read", bucket="results-bucket")
        credentials.client("s3", cross_usecase(), "live-expiry", bucket="results-bucket")
        assert [c["RoleSessionName"] for c in stubbed_clients.sts.calls] == [
            "dda-telemetry-api-control", "dda-telemetry-media-read", "dda-telemetry-live-expiry"]
        assert json.loads(stubbed_clients.sts.calls[0]["Policy"]) == sp.build("api-control")
        assert json.loads(stubbed_clients.sts.calls[1]["Policy"]) == sp.build(
            "media-read", usecase=cross_usecase(), bucket="results-bucket")
        assert json.loads(stubbed_clients.sts.calls[2]["Policy"]) == sp.build(
            "live-expiry", usecase=cross_usecase(), bucket="results-bucket")

    def test_root_marker_in_cross_topology_uses_default_credentials(self, stubbed_clients):
        from edge_telemetry_common import credentials
        usecase = cross_usecase(cross_account_role_arn="arn:aws:iam::{0}:root".format(CROSS_ACCOUNT))
        client = credentials.client("iot", usecase, "ingest")
        assert stubbed_clients.sts.calls == []
        assert client == ("client", "iot", {"region_name": REGION})


# --------------------------------------------------------------------------- devices
class _PagingTable:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def scan(self, **kwargs):
        self.calls.append(kwargs)
        index = kwargs.get("ExclusiveStartKey", {"page": 0})["page"]
        items = self.pages[index]
        response = {"Items": items}
        if index + 1 < len(self.pages):
            response["LastEvaluatedKey"] = {"page": index + 1}
        return response


class TestUsecaseDevices:
    def test_filtered_scan_against_the_moto_table(self, common, aws_stack):
        from edge_telemetry_common import devices
        usecase_id = "uc-{0}".format(uuid.uuid4())
        other = "uc-{0}".format(uuid.uuid4())
        for name in ("thing-b", "thing-a", "thing-c"):
            aws_stack.tables.devices.put_item(Item={"device_id": "{0}-{1}".format(name, usecase_id[-4:]),
                                                    "usecase_id": usecase_id, "target_architecture": "arm64_jp5"})
        aws_stack.tables.devices.put_item(Item={"device_id": "elsewhere-" + other[-4:], "usecase_id": other})
        aws_stack.tables.devices.put_item(Item={"device_id": "unrecorded-" + other[-4:]})
        suffix = usecase_id[-4:]
        assert devices.usecase_devices({"usecase_id": usecase_id}) == [
            "thing-a-" + suffix, "thing-b-" + suffix, "thing-c-" + suffix]
        assert devices.usecase_devices({"usecase_id": "uc-{0}".format(uuid.uuid4())}) == []

    def test_pagination_and_projection(self, common, monkeypatch):
        from edge_telemetry_common import devices
        table = _PagingTable([
            [{"device_id": "z-thing"}, {"device_id": "m-thing"}],
            [{"device_id": "a-thing"}],
            [{"device_id": "m-thing"}, {"other": 1}],
        ])
        monkeypatch.setattr(devices, "_table", lambda: table)
        assert devices.usecase_devices({"usecase_id": UC}) == ["a-thing", "m-thing", "z-thing"]
        assert len(table.calls) == 3
        assert table.calls[0]["ProjectionExpression"] == "device_id"
        assert "IndexName" not in table.calls[0]
        assert table.calls[1]["ExclusiveStartKey"] == {"page": 1}

    def test_sixty_second_cache(self, common, monkeypatch):
        from edge_telemetry_common import devices
        table = _PagingTable([[{"device_id": "one"}]])
        monkeypatch.setattr(devices, "_table", lambda: table)
        clock = _FakeClock()
        assert devices.usecase_devices({"usecase_id": UC}, clock=clock) == ["one"]
        clock.advance(59)
        assert devices.usecase_devices({"usecase_id": UC}, clock=clock) == ["one"]
        assert len(table.calls) == 1
        clock.advance(2)
        table.pages = [[{"device_id": "one"}, {"device_id": "two"}]]
        assert devices.usecase_devices({"usecase_id": UC}, clock=clock) == ["one", "two"]
        assert len(table.calls) == 2
        # The cached list is a copy: a caller cannot mutate the cache.
        devices.usecase_devices({"usecase_id": UC}, clock=clock).append("x")
        assert devices.usecase_devices({"usecase_id": UC}, clock=clock) == ["one", "two"]


# --------------------------------------------------------------------------- identity
def _client_error(code, status=400, operation="ListPrincipalThings"):
    return ClientError({"Error": {"Code": code, "Message": code},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


class _FakeIot:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def list_principal_things(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


PR = "ab" * 32


class TestIdentity:
    def test_strip_client_suffix(self, common):
        from edge_telemetry_common.identity import strip_client_suffix
        assert strip_client_suffix("mic-730-thing#3") == "mic-730-thing"
        assert strip_client_suffix("thing#12") == "thing"
        assert strip_client_suffix("thing") == "thing"
        assert strip_client_suffix("thing#") == "thing#"
        assert strip_client_suffix("th#ing") == "th#ing"
        assert strip_client_suffix(None) == ""

    def test_error_classification(self, common):
        from edge_telemetry_common.identity import classify_client_error
        assert classify_client_error(_client_error("ThrottlingException")) == "retryable"
        assert classify_client_error(_client_error("InternalFailureException", 500)) == "retryable"
        assert classify_client_error(_client_error("SomethingOdd", 503)) == "retryable"
        assert classify_client_error(ConnectTimeoutError(endpoint_url="https://iot.example.com")) == "retryable"
        assert classify_client_error(ReadTimeoutError(endpoint_url="https://iot.example.com")) == "retryable"
        assert classify_client_error(EndpointConnectionError(endpoint_url="https://iot.example.com")) == "retryable"
        assert classify_client_error(_client_error("AccessDeniedException", 403)) == "denied"
        assert classify_client_error(_client_error("AccessDenied", 403, "AssumeRole")) == "denied"
        assert classify_client_error(_client_error("UnauthorizedException", 401)) == "denied"
        assert classify_client_error(_client_error("ResourceNotFoundException", 404)) == "other"
        assert classify_client_error(_client_error("InvalidRequestException", 400)) == "other"
        assert classify_client_error(RuntimeError("x")) == "other"

    def test_resolves_one_thing_and_caches_it(self, common):
        from edge_telemetry_common import identity
        iot = _FakeIot([{"things": ["core-thing"]}])
        clock = _FakeClock()
        assert identity.resolve_thing(PR, cross_usecase(), clock=clock, iot_client=lambda: iot) == "core-thing"
        assert iot.calls[0]["principal"] == "arn:aws:iot:{0}:{1}:cert/{2}".format(REGION, CROSS_ACCOUNT, PR)
        clock.advance(59 * 60)
        assert identity.resolve_thing(PR.upper(), cross_usecase(), clock=clock, iot_client=lambda: iot) == "core-thing"
        assert len(iot.calls) == 1
        clock.advance(2 * 60)
        iot.answers.append({"things": ["core-thing"]})
        assert identity.resolve_thing(PR, cross_usecase(), clock=clock, iot_client=lambda: iot) == "core-thing"
        assert len(iot.calls) == 2

    def test_no_thing_or_several_things_is_unattributed_and_cached_five_minutes(self, common):
        from edge_telemetry_common import identity
        iot = _FakeIot([{"things": []}, {"things": ["a", "b"]}, {"things": ["a"]}])
        clock = _FakeClock()
        assert identity.resolve_thing(PR, cross_usecase(), clock=clock, iot_client=lambda: iot) is None
        clock.advance(4 * 60)
        assert identity.resolve_thing(PR, cross_usecase(), clock=clock, iot_client=lambda: iot) is None
        assert len(iot.calls) == 1
        clock.advance(2 * 60)
        assert identity.resolve_thing(PR, cross_usecase(), clock=clock, iot_client=lambda: iot) is None
        assert len(iot.calls) == 2, "two things: ambiguous, unattributed"
        clock.advance(6 * 60)
        assert identity.resolve_thing(PR, cross_usecase(), clock=clock, iot_client=lambda: iot) == "a"

    def test_pagination_is_followed(self, common):
        from edge_telemetry_common import identity
        iot = _FakeIot([{"things": [], "nextToken": "t1"}, {"things": ["only"]}])
        assert identity.resolve_thing(PR, cross_usecase(), iot_client=lambda: iot) == "only"
        assert iot.calls[1]["nextToken"] == "t1"

    def test_non_thumbprint_principal_is_unattributed_without_a_call(self, common):
        from edge_telemetry_common import identity
        iot = _FakeIot([])
        assert identity.resolve_thing("AIDAEXAMPLE:session", cross_usecase(), iot_client=lambda: iot) is None
        assert identity.resolve_thing(None, cross_usecase(), iot_client=lambda: iot) is None
        assert identity.resolve_thing("ab" * 31, cross_usecase(), iot_client=lambda: iot) is None
        assert iot.calls == []

    def test_throttling_and_timeouts_raise_retryable_and_cache_nothing(self, common):
        from edge_telemetry_common import identity
        iot = _FakeIot([_client_error("ThrottlingException"), ReadTimeoutError(endpoint_url="https://iot.example.com"),
                        _client_error("InternalFailureException", 500), {"things": ["core-thing"]}])
        for _ in range(3):
            with pytest.raises(identity.LookupRetryable):
                identity.resolve_thing(PR, cross_usecase(), iot_client=lambda: iot)
        assert identity.resolve_thing(PR, cross_usecase(), iot_client=lambda: iot) == "core-thing"
        assert len(iot.calls) == 4

    def test_access_denied_is_terminal_and_cached_five_minutes_per_account(self, common):
        from edge_telemetry_common import identity
        iot = _FakeIot([_client_error("AccessDeniedException", 403)])
        clock = _FakeClock()
        with pytest.raises(identity.LookupDenied):
            identity.resolve_thing(PR, cross_usecase(), clock=clock, iot_client=lambda: iot)
        clock.advance(60)
        with pytest.raises(identity.LookupDenied):
            identity.resolve_thing("cd" * 32, cross_usecase(), clock=clock, iot_client=lambda: iot)
        assert len(iot.calls) == 1, "the denial is cached per account"
        # Another account is not affected.
        other = _FakeIot([{"things": ["other-thing"]}])
        assert identity.resolve_thing(PR, cross_usecase(account_id="333333333333"), clock=clock,
                                      iot_client=lambda: other) == "other-thing"
        clock.advance(5 * 60)
        iot.answers.append({"things": ["core-thing"]})
        assert identity.resolve_thing(PR, cross_usecase(), clock=clock, iot_client=lambda: iot) == "core-thing"

    def test_denied_role_assumption_is_a_denied_lookup(self, common):
        from edge_telemetry_common import identity

        def failing_client():
            raise _client_error("AccessDenied", 403, "AssumeRole")

        with pytest.raises(identity.LookupDenied):
            identity.resolve_thing(PR, cross_usecase(), iot_client=failing_client)

    def test_other_client_errors_are_unattributed(self, common):
        from edge_telemetry_common import identity
        iot = _FakeIot([_client_error("ResourceNotFoundException", 404), _client_error("InvalidRequestException", 400)])
        assert identity.resolve_thing(PR, cross_usecase(), iot_client=lambda: iot) is None
        identity.clear_cache()
        assert identity.resolve_thing(PR, cross_usecase(), iot_client=lambda: iot) is None

    def test_default_client_comes_from_the_ingest_purpose(self, common, monkeypatch):
        from edge_telemetry_common import credentials, identity
        seen = []

        def fake_client(service, usecase, purpose, region=None, **kwargs):
            seen.append((service, purpose))
            return _FakeIot([{"things": ["core-thing"]}])

        monkeypatch.setattr(credentials, "client", fake_client)
        assert identity.resolve_thing(PR, cross_usecase(), iot_client=None) == "core-thing"
        assert seen == [("iot", "ingest")]


# --------------------------------------------------------------------------- shadow pool
class TestShadowPool:
    def test_constants(self, common):
        from edge_telemetry_common import shadow_pool
        assert shadow_pool.SHADOW_WRITE_SYNC_LIMIT == 40
        assert shadow_pool.WRITE_THREADS == 8
        assert shadow_pool.WRITE_CEILING_SECONDS == 2.0

    def test_result_shape_and_order(self, common):
        from edge_telemetry_common import shadow_pool

        def write(item):
            if item == "bad":
                raise RuntimeError("thing not found " + "x" * 300)
            return "ok:" + item

        results = shadow_pool.write_all(["a", "bad", "c"], write)
        assert [r.item for r in results] == ["a", "bad", "c"]
        assert results[0] == shadow_pool.WriteResult("a", True, "ok:a", None, False)
        assert results[1].ok is False and results[1].value is None and results[1].timed_out is False
        assert results[1].error.startswith("thing not found") and len(results[1].error) == 200
        assert results[2].value == "ok:c"
        assert shadow_pool.write_all([], write) == []

    def test_ceiling_bounds_a_slow_write(self, common):
        from edge_telemetry_common import shadow_pool
        release = threading.Event()

        def write(item):
            if item == "slow":
                release.wait(5)
                return "late"
            return "fast"

        started = time.monotonic()
        results = shadow_pool.write_all(["fast-1", "slow", "fast-2"], write, ceiling_seconds=0.2)
        elapsed = time.monotonic() - started
        release.set()
        assert elapsed < 2.0
        assert [r.ok for r in results] == [True, False, True]
        assert results[1].timed_out is True and results[1].error == "timeout"

    def test_runs_on_at_most_the_given_threads(self, common):
        from edge_telemetry_common import shadow_pool
        active = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def write(item):
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            time.sleep(0.02)
            with lock:
                active["now"] -= 1
            return item

        results = shadow_pool.write_all(list(range(20)), write, threads=3, ceiling_seconds=2.0)
        assert all(r.ok for r in results)
        assert [r.value for r in results] == list(range(20))
        assert 1 <= active["peak"] <= 3
