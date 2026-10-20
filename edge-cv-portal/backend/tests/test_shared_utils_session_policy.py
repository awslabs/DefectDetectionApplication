"""The optional ``session_policy`` keyword of ``assume_usecase_role`` and
``get_s3_client_for_bucket`` (workflow-debug-mode tasks.md 3.1;
workflow-telemetry-dashboards design §14.4).

Without the argument the STS call is byte-identical to before (no ``Policy``
key); with it the serialized policy travels as ``Policy``; the ``:root``
marker path never reaches STS in either case; ``get_s3_client_for_bucket``
forwards the keyword. Runs on the conftest moto stack so the real layer is
the module under test; ``boto3.client`` is replaced for the duration of each
case so the recorded ``assume_role`` parameters are the assertion.
"""
import json

import pytest

ROLE_ARN = "arn:aws:iam::222222222222:role/DDAPortalAccessRole"
ROOT_ARN = "arn:aws:iam::222222222222:root"
POLICY = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Action": "iot:ListPrincipalThings",
                   "Resource": "arn:aws:iot:*:*:cert/*"}],
}
CREDENTIALS = {
    "AccessKeyId": "ASIAFIXTUREKEYID0001",
    "SecretAccessKey": "fixture-secret",
    "SessionToken": "fixture-token",
}


class _RecordingSts:
    def __init__(self):
        self.calls = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        return {"Credentials": dict(CREDENTIALS)}


class _RecordingClientFactory:
    """Stands in for ``boto3.client`` inside ``shared_utils``: hands out one
    recording STS stub and records every other client request."""

    def __init__(self):
        self.sts = _RecordingSts()
        self.clients = []

    def __call__(self, service_name, *args, **kwargs):
        self.clients.append((service_name, args, kwargs))
        if service_name == "sts":
            return self.sts
        return ("client", service_name, kwargs)


@pytest.fixture
def shared(aws_stack):
    import shared_utils
    return shared_utils


@pytest.fixture
def recording(shared, monkeypatch):
    factory = _RecordingClientFactory()
    monkeypatch.setattr(shared.boto3, "client", factory)
    return factory


class TestAssumeUsecaseRole:
    def test_without_the_argument_no_policy_key_is_sent(self, shared, recording):
        creds = shared.assume_usecase_role(ROLE_ARN, "fixture-eid", "portal-session")
        assert creds == CREDENTIALS
        assert len(recording.sts.calls) == 1
        call = recording.sts.calls[0]
        assert "Policy" not in call
        assert call == {
            "RoleArn": ROLE_ARN,
            "RoleSessionName": "portal-session",
            "DurationSeconds": 3600,
            "ExternalId": "fixture-eid",
        }

    def test_explicit_none_is_the_same_as_absent(self, shared, recording):
        shared.assume_usecase_role(ROLE_ARN, "fixture-eid", "portal-session", session_policy=None)
        assert "Policy" not in recording.sts.calls[0]

    def test_with_the_argument_the_serialized_policy_is_sent(self, shared, recording):
        creds = shared.assume_usecase_role(ROLE_ARN, "fixture-eid", "dda-telemetry-ingest",
                                           session_policy=POLICY)
        assert creds == CREDENTIALS
        call = recording.sts.calls[0]
        assert call["Policy"] == json.dumps(POLICY)
        assert json.loads(call["Policy"]) == POLICY
        assert call["RoleArn"] == ROLE_ARN
        assert call["RoleSessionName"] == "dda-telemetry-ingest"
        assert call["ExternalId"] == "fixture-eid"
        assert call["DurationSeconds"] == 3600

    def test_without_external_id_the_key_stays_absent(self, shared, recording):
        shared.assume_usecase_role(ROLE_ARN, None, "portal-session", session_policy=POLICY)
        call = recording.sts.calls[0]
        assert "ExternalId" not in call
        assert "Policy" in call

    @pytest.mark.parametrize("policy", [None, POLICY])
    def test_root_marker_path_is_unchanged(self, shared, recording, policy):
        creds = shared.assume_usecase_role(ROOT_ARN, "fixture-eid", "portal-session",
                                           session_policy=policy)
        assert creds == {
            "AccessKeyId": None,
            "SecretAccessKey": None,
            "SessionToken": None,
            "is_default_credentials": True,
        }
        assert recording.sts.calls == []
        assert recording.clients == []


class TestGetS3ClientForBucket:
    USECASE = {
        "usecase_id": "uc-session-policy",
        "account_id": "222222222222",
        "cross_account_role_arn": ROLE_ARN,
        "external_id": "fixture-eid",
        "s3_bucket": "dda-inference-results-222222222222",
    }

    def test_forwards_no_policy_by_default(self, shared, recording):
        shared.get_s3_client_for_bucket(self.USECASE, "dda-inference-results-222222222222")
        assert len(recording.sts.calls) == 1
        assert "Policy" not in recording.sts.calls[0]
        assert recording.sts.calls[0]["RoleSessionName"] == "portal-s3-access"
        s3_requests = [c for c in recording.clients if c[0] == "s3"]
        assert len(s3_requests) == 1
        assert s3_requests[0][2]["aws_access_key_id"] == CREDENTIALS["AccessKeyId"]

    def test_forwards_the_policy_when_given(self, shared, recording):
        shared.get_s3_client_for_bucket(
            self.USECASE, "dda-inference-results-222222222222",
            session_name="dda-telemetry-media-retention-read",
            session_policy=POLICY)
        call = recording.sts.calls[0]
        assert call["Policy"] == json.dumps(POLICY)
        assert call["RoleSessionName"] == "dda-telemetry-media-retention-read"

    def test_bucket_binding_still_runs_first(self, shared, recording):
        with pytest.raises(shared.BucketNotAllowed):
            shared.get_s3_client_for_bucket(self.USECASE, "some-other-bucket",
                                            session_policy=POLICY)
        assert recording.sts.calls == []
