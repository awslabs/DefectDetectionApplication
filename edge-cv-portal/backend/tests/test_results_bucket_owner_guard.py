"""
S3 bucket-squatting regression tests for the DERIVED inference-results bucket
(``dda-inference-results-<use case account>``), the portal side.

A use case's default results bucket is named from its account id, and nothing
in the repository creates it (the operator does), so a third party who knows
the account id can create that exact name first. The portal would then list
and read that account's objects as the use case's captures, telemetry media,
debug images and tuning samples, and write the portal's lifecycle rules and
tuning manifests into it. Every portal S3 call against the derived default
now asserts the use case's account as ``ExpectedBucketOwner``; an
admin-configured override (``inference_uploader_s3_bucket``) is not a derived
name and carries no assertion (its admission is the bucket allowlist's).

Covers the shared helpers (``results_bucket_owner``, ``expected_owner_args``,
``OwnerAssertingS3``), the per-module ``owner_args`` of ``telemetry.media``
and ``workflow_debug.media``, the readiness probe, the debug HEAD, the tuning
Sample_Store proxy and the tuning lifecycle writer, each against a fake that
models S3's refusal (403) of a mismatched owner.
"""
import os
import sys
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

ACCOUNT = "222222222222"
SQUATTER = "999999999999"
DERIVED = "dda-inference-results-" + ACCOUNT


def client_error(code, operation, status=403):
    return ClientError({"Error": {"Code": code, "Message": code},
                        "ResponseMetadata": {"HTTPStatusCode": status, "HTTPHeaders": {}}}, operation)


def usecase(**overrides):
    record = {"usecase_id": "uc-results", "account_id": ACCOUNT, "region": "us-east-1",
              "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT}:role/DDAPortalAccessRole",
              "external_id": "ext"}
    record.update(overrides)
    return record


class OwnedS3:
    """A bucket owned by `owner`: a call that asserts another owner is refused
    with 403 before anything is read or written; every call is recorded with
    the owner it asserted (None = unasserted)."""

    def __init__(self, owner=ACCOUNT):
        self.owner = owner
        self.calls = []

    def _check(self, operation, kwargs):
        self.calls.append((operation, kwargs.get("Bucket"), kwargs.get("ExpectedBucketOwner")))
        expected = kwargs.get("ExpectedBucketOwner")
        if expected is not None and expected != self.owner:
            raise client_error("AccessDenied", operation)

    def list_objects_v2(self, **kwargs):
        self._check("ListObjectsV2", kwargs)
        return {"KeyCount": 0, "Contents": [], "ResponseMetadata": {
            "HTTPStatusCode": 200, "HTTPHeaders": {"x-amz-bucket-region": "eu-west-1"}}}

    def head_object(self, **kwargs):
        self._check("HeadObject", kwargs)
        return {"ContentLength": 3}

    def get_object(self, **kwargs):
        self._check("GetObject", kwargs)
        return {"Body": SimpleNamespace(read=lambda: b"{}")}

    def put_object(self, **kwargs):
        self._check("PutObject", kwargs)
        return {}

    def copy_object(self, **kwargs):
        self._check("CopyObject", kwargs)
        self.calls[-1] = self.calls[-1] + (kwargs.get("ExpectedSourceBucketOwner"),)
        return {}

    def get_bucket_lifecycle_configuration(self, **kwargs):
        self._check("GetBucketLifecycleConfiguration", kwargs)
        return {"Rules": []}

    def put_bucket_lifecycle_configuration(self, **kwargs):
        self._check("PutBucketLifecycleConfiguration", kwargs)
        return {}

    def get_bucket_versioning(self, **kwargs):
        self._check("GetBucketVersioning", kwargs)
        return {}

    def generate_presigned_url(self, operation, Params=None, ExpiresIn=None):
        self.calls.append(("Presign", Params.get("Bucket"), Params.get("ExpectedBucketOwner")))
        return "https://signed.example/" + Params["Key"]

    def get_paginator(self, name):
        outer = self

        class _Paginator:
            def paginate(self, **kwargs):
                outer._check("ListObjectsV2(paginate)", kwargs)
                yield {"Contents": []}

        return _Paginator()


# ---------------------------------------------------------------------------
# shared_utils helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def shared(aws_stack):
    import shared_utils
    assert hasattr(shared_utils, "results_bucket_owner"), "this suite needs the real shared layer"
    return shared_utils


def test_results_bucket_owner_is_the_use_case_account_for_the_derived_default_only(shared):
    assert shared.results_bucket_owner(usecase(), DERIVED) == ACCOUNT
    assert shared.results_bucket_owner(usecase(), "customer-results") is None
    assert shared.results_bucket_owner(usecase(), "dda-inference-results-" + SQUATTER) is None
    assert shared.results_bucket_owner(usecase(account_id=""), "dda-inference-results-") is None
    assert shared.results_bucket_owner(None, DERIVED) is None
    assert shared.expected_owner_args(ACCOUNT) == {"ExpectedBucketOwner": ACCOUNT}
    assert shared.expected_owner_args(None) == {}


def test_derived_results_bucket_owner_reads_the_account_from_the_name(shared):
    assert shared.derived_results_bucket_owner(DERIVED) == ACCOUNT
    assert shared.derived_results_bucket_owner("customer-results") is None
    assert shared.derived_results_bucket_owner("dda-inference-results-12") is None
    assert shared.derived_results_bucket_owner(None) is None


class TestOwnerAssertingS3:
    def test_asserts_the_owner_on_every_call_addressed_to_the_bucket(self, shared):
        inner = OwnedS3(owner=ACCOUNT)
        proxy = shared.OwnerAssertingS3(inner, DERIVED, ACCOUNT)
        proxy.list_objects_v2(Bucket=DERIVED, Prefix="p/", MaxKeys=1)
        proxy.get_object(Bucket=DERIVED, Key="k")
        proxy.put_object(Bucket=DERIVED, Key="k", Body=b"x")
        proxy.copy_object(Bucket=DERIVED, Key="k2", CopySource={"Bucket": DERIVED, "Key": "k"})
        list(proxy.get_paginator("list_objects_v2").paginate(Bucket=DERIVED, Prefix="p/"))
        assert inner.calls == [
            ("ListObjectsV2", DERIVED, ACCOUNT), ("GetObject", DERIVED, ACCOUNT),
            ("PutObject", DERIVED, ACCOUNT), ("CopyObject", DERIVED, ACCOUNT, ACCOUNT),
            ("ListObjectsV2(paginate)", DERIVED, ACCOUNT)]

    def test_a_presigned_url_and_other_buckets_pass_through_unasserted(self, shared):
        inner = OwnedS3(owner=ACCOUNT)
        proxy = shared.OwnerAssertingS3(inner, DERIVED, ACCOUNT)
        proxy.generate_presigned_url("get_object", Params={"Bucket": DERIVED, "Key": "k"}, ExpiresIn=60)
        proxy.get_object(Bucket="another-bucket", Key="k")
        assert inner.calls == [("Presign", DERIVED, None), ("GetObject", "another-bucket", None)]

    def test_caller_supplied_owner_is_kept_and_no_owner_is_transparent(self, shared):
        inner = OwnedS3(owner=ACCOUNT)
        shared.OwnerAssertingS3(inner, DERIVED, ACCOUNT).get_object(
            Bucket=DERIVED, Key="k", ExpectedBucketOwner=ACCOUNT)
        shared.OwnerAssertingS3(inner, "customer-results", None).get_object(Bucket="customer-results", Key="k")
        assert inner.calls == [("GetObject", DERIVED, ACCOUNT), ("GetObject", "customer-results", None)]

    def test_a_squatted_bucket_is_refused_on_the_first_call(self, shared):
        inner = OwnedS3(owner=SQUATTER)
        proxy = shared.OwnerAssertingS3(inner, DERIVED, ACCOUNT)
        with pytest.raises(ClientError):
            proxy.put_object(Bucket=DERIVED, Key="k", Body=b"x")
        assert inner.calls == [("PutObject", DERIVED, ACCOUNT)]


# ---------------------------------------------------------------------------
# telemetry.media / telemetry.readiness
# ---------------------------------------------------------------------------

@pytest.fixture
def telemetry(aws_stack):
    from telemetry import media, readiness
    return SimpleNamespace(media=media, readiness=readiness)


def test_telemetry_owner_args_follow_the_results_bucket(telemetry):
    assert telemetry.media.owner_args(usecase(), DERIVED) == {"ExpectedBucketOwner": ACCOUNT}
    override = usecase(inference_uploader_s3_bucket="customer-results")
    assert telemetry.media.results_bucket(override) == "customer-results"
    assert telemetry.media.owner_args(override, "customer-results") == {}


def test_media_location_probe_asserts_the_owner_and_reports_a_squatted_bucket_denied(telemetry):
    owned = OwnedS3(owner=ACCOUNT)
    probe = telemetry.readiness.probe_media_location(usecase(), DERIVED, client=owned)
    assert probe.ok and probe.region == "eu-west-1" and probe.region_source == "header"
    assert owned.calls == [("ListObjectsV2", DERIVED, ACCOUNT)]

    squatted = OwnedS3(owner=SQUATTER)
    probe = telemetry.readiness.probe_media_location(usecase(), DERIVED, client=squatted)
    assert not probe.ok and probe.error == "AccessDenied"
    assert squatted.calls == [("ListObjectsV2", DERIVED, ACCOUNT)]

    unasserted = OwnedS3(owner=SQUATTER)
    override = usecase(inference_uploader_s3_bucket="customer-results")
    probe = telemetry.readiness.probe_media_location(override, "customer-results", client=unasserted)
    assert probe.ok
    assert unasserted.calls == [("ListObjectsV2", "customer-results", None)]


# ---------------------------------------------------------------------------
# workflow_debug.media
# ---------------------------------------------------------------------------

@pytest.fixture
def debug_media(aws_stack):
    from workflow_debug import media
    return media


def test_debug_media_asserts_the_owner_on_probe_readiness_and_head(debug_media):
    owned = OwnedS3(owner=ACCOUNT)
    probe = debug_media.probe_region(usecase(), DERIVED, client=owned)
    assert probe.region == "eu-west-1"
    assert debug_media.images_ready(usecase(), DERIVED, client=owned).reason == "rule_absent"
    assert debug_media.head_image(usecase(), DERIVED, "k", client=owned) is True
    debug_media.presign_image(usecase(), DERIVED, "k", client=owned)
    assert owned.calls == [
        ("ListObjectsV2", DERIVED, ACCOUNT), ("GetBucketLifecycleConfiguration", DERIVED, ACCOUNT),
        ("HeadObject", DERIVED, ACCOUNT), ("Presign", DERIVED, None)]


def test_debug_media_fails_closed_on_a_squatted_bucket(debug_media):
    squatted = OwnedS3(owner=SQUATTER)
    # The probe falls back to the use case's region (no header from a refusal)
    # and the image is reported absent rather than served from the squatter.
    assert debug_media.probe_region(usecase(), DERIVED, client=squatted).region_source == "usecase"
    assert debug_media.images_ready(usecase(), DERIVED, client=squatted).reason == "lifecycle_unreadable"
    assert debug_media.head_image(usecase(), DERIVED, "k", client=squatted) is False
    assert all(call[2] == ACCOUNT for call in squatted.calls)


# ---------------------------------------------------------------------------
# workflow_tuning.sample_store / deployments.apply_workflow_tuning_lifecycle
# ---------------------------------------------------------------------------

@pytest.fixture
def tuning(aws_stack, monkeypatch):
    monkeypatch.setenv("WORKFLOW_TUNING_TABLE", "test-workflow-tuning-owner")
    for name in ("workflow_tuning",):
        sys.modules.pop(name, None)
    import workflow_tuning
    return workflow_tuning


def test_sample_store_client_asserts_the_owner_of_the_derived_default(tuning, monkeypatch):
    inner = OwnedS3(owner=ACCOUNT)
    monkeypatch.setattr(tuning, "get_usecase_client", lambda *args, **kwargs: inner)
    client, bucket = tuning.sample_store(usecase())
    assert bucket == DERIVED
    keys, truncated = tuning.list_objects(client, bucket, "workflow-tuning/samples/")
    assert keys == {} and truncated is False
    tuning.read_sample_bytes(client, bucket, "k")
    client.put_object(Bucket=bucket, Key="m", Body=b"{}", ContentType="application/json")
    tuning.presign(client, bucket, "k")
    assert inner.calls == [
        ("ListObjectsV2", DERIVED, ACCOUNT), ("GetObject", DERIVED, ACCOUNT),
        ("PutObject", DERIVED, ACCOUNT), ("Presign", DERIVED, None)]


def test_sample_store_refuses_a_squatted_default_and_leaves_an_override_alone(tuning, monkeypatch):
    squatted = OwnedS3(owner=SQUATTER)
    monkeypatch.setattr(tuning, "get_usecase_client", lambda *args, **kwargs: squatted)
    client, bucket = tuning.sample_store(usecase())
    with pytest.raises(ClientError):
        tuning.read_sample_bytes(client, bucket, "k")
    assert squatted.calls == [("GetObject", DERIVED, ACCOUNT)]

    unasserted = OwnedS3(owner=SQUATTER)
    monkeypatch.setattr(tuning, "get_usecase_client", lambda *args, **kwargs: unasserted)
    client, bucket = tuning.sample_store(usecase(inference_uploader_s3_bucket="customer-results"))
    assert bucket == "customer-results"
    tuning.read_sample_bytes(client, bucket, "k")
    assert unasserted.calls == [("GetObject", "customer-results", None)]


@pytest.fixture
def deployments(aws_stack):
    for name in ("deployments", "workflow_guards"):
        sys.modules.pop(name, None)
    import deployments
    return deployments


def test_tuning_lifecycle_writer_asserts_the_owner_and_fails_closed(deployments, monkeypatch):
    owned = OwnedS3(owner=ACCOUNT)
    monkeypatch.setattr(deployments, "get_usecase_client", lambda *args, **kwargs: owned)
    result = deployments.apply_workflow_tuning_lifecycle(usecase(), DERIVED)
    assert result["status"] == "applied"
    assert owned.calls == [("GetBucketLifecycleConfiguration", DERIVED, ACCOUNT),
                           ("PutBucketLifecycleConfiguration", DERIVED, ACCOUNT)]

    squatted = OwnedS3(owner=SQUATTER)
    monkeypatch.setattr(deployments, "get_usecase_client", lambda *args, **kwargs: squatted)
    result = deployments.apply_workflow_tuning_lifecycle(usecase(), DERIVED)
    assert result["status"] == "failed" and "AccessDenied" in result["error"]
    assert squatted.calls == [("GetBucketLifecycleConfiguration", DERIVED, ACCOUNT)]

    unasserted = OwnedS3(owner=SQUATTER)
    monkeypatch.setattr(deployments, "get_usecase_client", lambda *args, **kwargs: unasserted)
    result = deployments.apply_workflow_tuning_lifecycle(
        usecase(inference_uploader_s3_bucket="customer-results"), "customer-results")
    assert result["status"] == "applied"
    assert unasserted.calls[0] == ("GetBucketLifecycleConfiguration", "customer-results", None)
