"""``telemetry.media`` (workflow-telemetry-dashboards design §12.1-§12.3;
tasks.md 3.4): every refusal reason, the Results_Bucket precedence, and no
credential before binding (asserted by a spy on the credential chooser)."""
import sys

import pytest

UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF = "7a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
DEVICE = "mic-730-demo"
ACCOUNT = "222222222222"


@pytest.fixture
def media(aws_stack, monkeypatch):
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", "111111111111")
    monkeypatch.setenv("PORTAL_ARTIFACTS_BUCKET", "edgecv-portal-artifacts-fixture")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    for name in list(sys.modules):
        if name in ("telemetry",) or name.startswith("telemetry.") or name.startswith("edge_telemetry_common"):
            sys.modules.pop(name, None)
    from telemetry import media as module
    return module


def usecase(**over):
    base = {"usecase_id": UC, "account_id": ACCOUNT, "region": "us-east-1"}
    base.update(over)
    return base


def record():
    return {"usecase_id": UC, "device_id": DEVICE, "workflow_id": WF}


class TestResultsBucket:
    def test_default_and_override(self, media):
        assert media.results_bucket(usecase()) == "dda-inference-results-" + ACCOUNT
        assert media.results_bucket(usecase(inference_uploader_s3_bucket="my-results")) == "my-results"
        assert media.results_bucket(usecase(inference_uploader_s3_bucket="")) == "dda-inference-results-" + ACCOUNT

    def test_media_location(self, media):
        assert media.media_location(usecase()) == {"bucket": "dda-inference-results-" + ACCOUNT,
                                                   "prefix": "dda-telemetry/", "source": "default"}
        assert media.media_location(usecase(inference_uploader_s3_bucket="my-results"))["source"] == "override"

    def test_pair_prefixes(self, media):
        assert media.pair_prefix("clips", UC, DEVICE, WF) == "dda-telemetry/clips/{0}/{1}/{2}/".format(UC, DEVICE, WF)
        assert media.pair_prefix("live", UC, DEVICE, WF) == "dda-telemetry/live/{0}/{1}/{2}/".format(UC, DEVICE, WF)


class TestBind:
    def test_a_bound_reference(self, media):
        key = "dda-telemetry/clips/{0}/{1}/{2}/2026-10-18/clip-1.mp4".format(UC, DEVICE, WF)
        binding = media.bind(usecase(), record(), "s3://dda-inference-results-{0}/{1}".format(ACCOUNT, key))
        assert binding.ok and binding.bucket == "dda-inference-results-" + ACCOUNT and binding.key == key
        live = media.bind_key(usecase(), "live", UC, DEVICE, WF, "dda-inference-results-" + ACCOUNT,
                              "dda-telemetry/live/{0}/{1}/{2}/3.jpg".format(UC, DEVICE, WF))
        assert live.ok

    def test_invalid_s3_uri(self, media):
        for uri in ("https://bucket/key", "s3://", "s3://bucket", "s3://Bad Bucket/key", None, 12, "s3://bucket@other/key"):
            binding = media.bind(usecase(), record(), uri)
            assert binding.reason == "invalid_s3_uri" and not binding.ok, uri
        assert media.bind_key(usecase(), "clips", UC, DEVICE, WF, "ok-bucket", "").reason == "invalid_s3_uri"
        assert media.bind_key(usecase(), "clips", UC, DEVICE, WF, "", "k").reason == "invalid_s3_uri"

    def test_bucket_not_allowed(self, media):
        key = "dda-telemetry/clips/{0}/{1}/{2}/2026-10-18/clip-1.mp4".format(UC, DEVICE, WF)
        binding = media.bind(usecase(), record(), "s3://someone-elses-bucket/" + key)
        assert binding.reason == "bucket_not_allowed" and binding.detail == "not_results_bucket"
        portal = media.bind(usecase(), record(), "s3://edgecv-portal-artifacts-fixture/" + key)
        assert portal.reason == "bucket_not_allowed" and portal.detail == "portal_bucket"
        lake = media.bind(usecase(), record(), "s3://edgecv-portal-telemetry-111111111111-us-east-1/" + key)
        assert lake.reason == "bucket_not_allowed" and lake.detail == "portal_bucket"
        # The default bucket is refused once an override is set (the precedence of captures.py).
        overridden = media.bind(usecase(inference_uploader_s3_bucket="my-results"), record(),
                                "s3://dda-inference-results-{0}/{1}".format(ACCOUNT, key))
        assert overridden.reason == "bucket_not_allowed"
        assert media.bind(usecase(inference_uploader_s3_bucket="my-results"), record(), "s3://my-results/" + key).ok

    def test_key_outside_prefix(self, media):
        bucket = "dda-inference-results-" + ACCOUNT
        other_device = "dda-telemetry/clips/{0}/other/{1}/2026-10-18/c.mp4".format(UC, WF)
        other_usecase = "dda-telemetry/clips/{0}/{1}/{2}/c.mp4".format("00000000-0000-4000-8000-00000000000a", DEVICE, WF)
        traversal = "dda-telemetry/clips/{0}/{1}/{2}/../x/c.mp4".format(UC, DEVICE, WF)
        folder = "dda-telemetry/clips/{0}/{1}/{2}/".format(UC, DEVICE, WF)
        live_as_clip = "dda-telemetry/live/{0}/{1}/{2}/3.jpg".format(UC, DEVICE, WF)
        for key in (other_device, other_usecase, traversal, folder, live_as_clip, "captures/x.jpg"):
            binding = media.bind(usecase(), record(), "s3://{0}/{1}".format(bucket, key))
            assert binding.reason == "key_outside_prefix", key
            assert binding.bucket == bucket and binding.key is None

    def test_no_credential_before_binding(self, media, monkeypatch):
        from edge_telemetry_common import credentials
        import shared_utils
        calls = []
        monkeypatch.setattr(credentials, "client", lambda *a, **k: calls.append(("credentials", a)) or None)
        monkeypatch.setattr(shared_utils, "assume_usecase_role", lambda *a, **k: calls.append(("sts", a)) or {})
        monkeypatch.setattr(shared_utils.boto3, "client", lambda *a, **k: calls.append(("boto3", a)) or None)
        media.bind(usecase(), record(), "s3://someone-elses-bucket/x")
        media.bind(usecase(), record(), "s3://dda-inference-results-{0}/outside".format(ACCOUNT))
        media.bind(usecase(), record(), "not-a-uri")
        media.bind(usecase(), record(), "s3://dda-inference-results-{0}/dda-telemetry/clips/{1}/{2}/{3}/d/c.mp4".format(
            ACCOUNT, UC, DEVICE, WF))
        assert calls == [], "binding makes no credential, STS or S3 call"

    def test_audit_only_for_an_allowed_set_bucket(self, media, aws_stack, monkeypatch):
        import shared_utils
        audited = []
        monkeypatch.setattr(shared_utils, "assert_bucket_allowed",
                            lambda u, b, purpose, **k: audited.append((b, purpose)) or None)
        key = "dda-telemetry/clips/{0}/{1}/{2}/2026-10-18/clip-1.mp4".format(UC, DEVICE, WF)
        media.bind(usecase(additional_s3_buckets=["customer-data"]), record(), "s3://customer-data/" + key, audit=True)
        assert audited == [("customer-data", "telemetry-media")]
        media.bind(usecase(), record(), "s3://someone-elses-bucket/" + key, audit=True)
        media.bind(usecase(), record(), "s3://dda-inference-results-{0}/{1}".format(ACCOUNT, key), audit=True)
        assert len(audited) == 1, "only an Allowed_Bucket_Set bucket goes through assert_bucket_allowed"
        media.bind(usecase(additional_s3_buckets=["customer-data"]), record(), "s3://customer-data/" + key, audit=False)
        assert len(audited) == 1, "the ingest never audits"
