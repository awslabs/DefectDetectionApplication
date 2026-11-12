"""
S3 bucket-squatting regression tests for the DERIVED component bucket name
(dda-component-<region>-<portal account>).

The portal derives the Greengrass component bucket's name from its own
account id and region. S3 bucket names are global, so a third party who
knows the account id can pre-create that exact name in another account;
without an ownership assertion every handler would then read from / write
to the attacker's bucket (bucket policy writers, the static-image pin
Image_Transport). The fix asserts ``ExpectedBucketOwner`` (the portal's own
account) on every S3 call against the derived name and treats a bucket of
that name owned by another account as a hard, reported failure, never as a
bucket to adopt.

These tests simulate the pre-existing attacker-owned bucket with fakes that
model S3's ExpectedBucketOwner semantics (403 AccessDenied when the asserted
owner differs from the bucket's owner) and prove each code path fails
closed: nothing is written, the failure names the ownership conflict, and
every call that does run carries the assertion.

Covers:
- shared_utils.component_bucket_owner(): PORTAL_ACCOUNT_ID, the STS caller
  identity fallback (cached), and the fail-closed BucketOwnerUnknown error.
- usecases.update_component_bucket_policy_for_greengrass /
  _remove_component_bucket_grants and
  shared_components.add_usecase_to_component_bucket_policy against a
  squatted bucket.
- camera_registry pin routes: the presigned staging PUT signs
  x-amz-expected-bucket-owner (and the response hands the header to the
  browser); the pin submit refuses a squatted Image_Transport before any
  Pin_Request or Sync_Channel write; the happy path asserts the owner on
  every S3 call.
"""
import json
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.exceptions import ClientError

import usecase_account_fakes as fx
from usecase_account_fakes import PORTAL, TRUSTED, COMPONENT_BUCKET, access_role, client_error
from pin_route_helpers import (  # noqa: F401 — pin_env fixture import
    FakeIotDataClient, RecordingS3, create_usecase, image_bytes, invoke,
    make_user, pin_env, pin_request_items, register_device, stage_object,
)

# The account that pre-created the derived name (the attacker's account).
SQUATTER = "999999999999"


# ---------------------------------------------------------------------------
# shared_utils.component_bucket_owner
# ---------------------------------------------------------------------------

@pytest.fixture
def shared(aws_stack):
    import shared_utils
    assert hasattr(shared_utils, "component_bucket_owner"), "this suite needs the real shared layer"
    shared_utils._caller_account_id_cache.clear()
    yield shared_utils
    shared_utils._caller_account_id_cache.clear()


def test_owner_is_the_portal_account_when_configured(shared, monkeypatch):
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL)
    assert shared.component_bucket_owner() == PORTAL


def test_owner_falls_back_to_the_caller_identity_and_caches_it(shared, monkeypatch):
    """Outside Lambda (PORTAL_ACCOUNT_ID unset) the owner is the caller's own
    account from STS, resolved once."""
    monkeypatch.delenv("PORTAL_ACCOUNT_ID", raising=False)
    calls = []
    real_client = shared.boto3.client

    def counting_client(service, **kwargs):
        if service == "sts":
            calls.append(service)
        return real_client(service, **kwargs)

    monkeypatch.setattr(shared.boto3, "client", counting_client)
    assert shared.component_bucket_owner() == "123456789012"  # moto's account
    assert shared.component_bucket_owner() == "123456789012"
    assert calls == ["sts"]


def test_unknown_owner_fails_closed(shared, monkeypatch):
    """No PORTAL_ACCOUNT_ID and no STS -> BucketOwnerUnknown, never an
    unguarded S3 call."""
    monkeypatch.delenv("PORTAL_ACCOUNT_ID", raising=False)

    def failing_client(service, **kwargs):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(shared.boto3, "client", failing_client)
    with pytest.raises(shared.BucketOwnerUnknown):
        shared.component_bucket_owner()


def test_is_bucket_owner_mismatch_recognizes_the_403_and_nothing_else(shared):
    assert shared.is_bucket_owner_mismatch(
        client_error("AccessDenied", status=403, operation="GetBucketPolicy"))
    assert shared.is_bucket_owner_mismatch(
        client_error("Forbidden", status=403, operation="HeadObject"))
    assert not shared.is_bucket_owner_mismatch(
        client_error("NoSuchBucketPolicy", status=404, operation="GetBucketPolicy"))
    assert not shared.is_bucket_owner_mismatch(RuntimeError("boom"))


# ---------------------------------------------------------------------------
# Bucket policy writers (usecases.py / shared_components.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def modules(aws_stack):
    import shared_utils
    mp = pytest.MonkeyPatch()
    mp.setenv("PORTAL_ACCOUNT_ID", PORTAL)
    mp.setenv("AWS_REGION", "us-east-1")
    mp.delenv("COMPONENT_BUCKET", raising=False)
    for name in ("shared_components", "usecases"):
        sys.modules.pop(name, None)
    import shared_components
    import usecases
    yield SimpleNamespace(shared=shared_utils, sc=shared_components, uc=usecases)
    mp.undo()


@pytest.fixture
def squatted(modules, monkeypatch):
    """The component bucket name exists, but under the squatter's account."""
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL)
    log = fx.OperationLog()
    s3 = fx.FakeComponentS3(log, owner=SQUATTER)
    monkeypatch.setattr(modules.uc, "boto3", SimpleNamespace(
        client=lambda service, **kwargs: s3 if service == "s3" else fx.boto3.client(service, **kwargs),
        resource=fx.boto3.resource))
    monkeypatch.setattr(modules.sc, "s3", s3)
    return s3


def test_grant_writer_refuses_a_squatted_component_bucket(modules, squatted):
    result = modules.uc.update_component_bucket_policy_for_greengrass(
        component_bucket_name=COMPONENT_BUCKET, device_account_id=TRUSTED)
    assert result["status"] == "failed"
    assert "ownership conflict" in result["error"]
    assert "bucket squatting" in result["error"]
    assert PORTAL in result["error"] and COMPONENT_BUCKET in result["error"]
    # Refused on the first call; nothing was written.
    assert [c[0] for c in squatted.calls] == ["get_bucket_policy"]
    assert squatted.asserted_owners == [PORTAL]
    assert squatted.policies == {}


def test_grant_remover_refuses_a_squatted_component_bucket(modules, squatted):
    result = modules.uc._remove_component_bucket_grants(COMPONENT_BUCKET, TRUSTED)
    assert result["status"] == "failed"
    assert "AccessDenied" in result["error"]
    assert [c[0] for c in squatted.calls] == ["get_bucket_policy"]
    assert squatted.asserted_owners == [PORTAL]
    assert squatted.policies == {}


def test_shared_components_writer_refuses_a_squatted_component_bucket(modules, squatted):
    assert modules.sc.add_usecase_to_component_bucket_policy(access_role(TRUSTED)) is False
    assert [c[0] for c in squatted.calls] == ["get_bucket_policy"]
    assert squatted.asserted_owners == [PORTAL]
    assert squatted.policies == {}


def test_writers_assert_the_portal_account_on_every_call_when_the_bucket_is_theirs(
        modules, monkeypatch):
    """The happy path: the bucket is the portal's, and both the read and the
    write of each writer carry ExpectedBucketOwner=<portal account>."""
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL)
    log = fx.OperationLog()
    s3 = fx.FakeComponentS3(log, owner=PORTAL)
    monkeypatch.setattr(modules.uc, "boto3", SimpleNamespace(
        client=lambda service, **kwargs: s3 if service == "s3" else fx.boto3.client(service, **kwargs),
        resource=fx.boto3.resource))
    monkeypatch.setattr(modules.sc, "s3", s3)

    added = modules.uc.update_component_bucket_policy_for_greengrass(
        component_bucket_name=COMPONENT_BUCKET, device_account_id=TRUSTED)
    assert added["status"] == "success"
    assert modules.sc.add_usecase_to_component_bucket_policy(access_role(TRUSTED)) is True
    removed = modules.uc._remove_component_bucket_grants(COMPONENT_BUCKET, TRUSTED)
    assert removed["status"] == "success"

    assert len(s3.calls) == 6  # get+put for each of the three writers
    assert s3.asserted_owners == [PORTAL] * 6
    assert None not in s3.asserted_owners


# ---------------------------------------------------------------------------
# camera_registry pin routes (the Image_Transport is the component bucket)
# ---------------------------------------------------------------------------

class SquattedS3:
    """An S3 whose buckets are all owned by the squatter: every call that
    asserts a different owner is refused with 403, exactly as S3 does, and
    nothing is read or written. Unasserted calls are recorded as such (and
    refused too, so an unguarded call can never look like success here).
    With ``head_ok`` the HeadObject succeeds (a bucket policy that lets the
    portal role see object metadata), so the refusal lands on GetObject."""

    def __init__(self, head_ok=False):
        self.calls = []
        self.head_ok = head_ok

    def _refuse(self, operation, kwargs):
        self.calls.append((operation, kwargs.get("ExpectedBucketOwner")))
        raise client_error("AccessDenied", "Access Denied", status=403, operation=operation)

    def head_object(self, **kwargs):
        if self.head_ok:
            self.calls.append(("HeadObject", kwargs.get("ExpectedBucketOwner")))
            return {"ContentLength": 64}
        self._refuse("HeadObject", kwargs)

    def get_object(self, **kwargs):
        self._refuse("GetObject", kwargs)

    def copy_object(self, **kwargs):
        self._refuse("CopyObject", kwargs)

    def delete_object(self, **kwargs):
        self._refuse("DeleteObject", kwargs)

    def generate_presigned_url(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("not expected on the submit route")


@pytest.fixture(scope="module")
def operator(pin_env):
    usecase_id = create_usecase(pin_env)
    return make_user("Operator"), usecase_id


@pytest.fixture
def portal_owner(monkeypatch):
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL)
    return PORTAL


def test_upload_url_signs_the_expected_bucket_owner_header(pin_env, operator, portal_owner):
    """The presigned staging PUT asserts the owner: x-amz-expected-bucket-owner
    is a signed header, and the response hands the browser the exact header
    to send (S3 then refuses the PUT if another account owns the bucket)."""
    import boto3
    # The module's own client configuration (SigV4 query presign), so the
    # URL has the production shape.
    sigv4_client = boto3.client("s3", region_name="us-east-1",
                                config=pin_env.module.PIN_S3_CLIENT_CONFIG)
    fake_s3 = RecordingS3(sigv4_client)
    pin_env.s3_holder["client"] = fake_s3
    pin_env.shadow_holder["client"] = FakeIotDataClient()
    user, usecase_id = operator
    device_id = register_device(pin_env, usecase_id)

    status, body = invoke(pin_env, "POST", device_id, user, sub_path="/static-image/upload-url")
    assert status == 200
    assert body["expectedBucketOwner"] == PORTAL
    assert body["uploadHeaders"] == {"x-amz-expected-bucket-owner": PORTAL}
    query = parse_qs(urlparse(body["uploadUrl"]).query)
    assert "x-amz-expected-bucket-owner" in query["X-Amz-SignedHeaders"][0]

    # The video route issues the same presign.
    status, body = invoke(pin_env, "POST", device_id, user, sub_path="/static-video/upload-url")
    assert status == 200
    assert body["uploadHeaders"] == {"x-amz-expected-bucket-owner": PORTAL}


def test_pin_submit_refuses_a_squatted_image_transport(pin_env, operator, portal_owner, caplog):
    """A pre-existing bucket of the derived name owned by another account:
    S3 refuses the owner-asserted HeadObject (403), the submit stops on that
    very first call with the route's 400 (the pin role has no s3:ListBucket,
    so a 403 is also what a missing staging key answers), the conflict is
    logged, and neither a Pin_Request nor the device shadow is written."""
    fake_s3 = SquattedS3()
    fake_shadow = FakeIotDataClient()
    pin_env.s3_holder["client"] = fake_s3
    pin_env.shadow_holder["client"] = fake_shadow
    user, usecase_id = operator
    device_id = register_device(pin_env, usecase_id)
    staging_key = stage_object(pin_env, image_bytes())

    status, body = invoke(pin_env, "POST", device_id, user, sub_path="/static-image/pin",
                          body={"stagingKey": staging_key, "fileName": "sample.png"})
    assert status == 400
    assert fake_s3.calls == [("HeadObject", PORTAL)]
    assert "bucket squatting" in caplog.text and PORTAL in caplog.text
    assert pin_request_items(pin_env, device_id) == []
    assert fake_shadow.updates == []


def test_pin_submit_names_the_conflict_when_the_staged_object_was_found(pin_env, operator,
                                                                        portal_owner):
    """A squatted bucket whose policy lets the portal see object metadata:
    HeadObject passes, the owner-asserted GetObject is refused, and the
    submit fails closed with the 502 that names the ownership conflict."""
    fake_s3 = SquattedS3(head_ok=True)
    fake_shadow = FakeIotDataClient()
    pin_env.s3_holder["client"] = fake_s3
    pin_env.shadow_holder["client"] = fake_shadow
    user, usecase_id = operator
    device_id = register_device(pin_env, usecase_id)
    staging_key = stage_object(pin_env, image_bytes())

    status, body = invoke(pin_env, "POST", device_id, user, sub_path="/static-image/pin",
                          body={"stagingKey": staging_key, "fileName": "sample.png"})
    assert status == 502
    assert "ownership conflict" in body["error"]
    assert fake_s3.calls == [("HeadObject", PORTAL), ("GetObject", PORTAL)]
    assert pin_request_items(pin_env, device_id) == []
    assert fake_shadow.updates == []


def test_video_pin_submit_refuses_a_squatted_image_transport(pin_env, operator, portal_owner,
                                                             caplog):
    fake_s3 = SquattedS3()
    fake_shadow = FakeIotDataClient()
    pin_env.s3_holder["client"] = fake_s3
    pin_env.shadow_holder["client"] = fake_shadow
    user, usecase_id = operator
    device_id = register_device(pin_env, usecase_id)
    staging_key = stage_object(pin_env, b"\x00" * 64)

    status, body = invoke(pin_env, "POST", device_id, user, sub_path="/static-video/pin",
                          body={"stagingKey": staging_key, "fileName": "loop.mp4"})
    assert status == 400
    assert fake_s3.calls == [("HeadObject", PORTAL)]
    assert "bucket squatting" in caplog.text
    assert fake_shadow.updates == []


def test_pin_submit_asserts_the_owner_on_every_image_transport_call(pin_env, operator,
                                                                    portal_owner):
    """The happy path against the portal's own bucket: HeadObject, GetObject,
    CopyObject (destination AND source) all carry the portal account."""

    class AssertingS3(RecordingS3):
        def __init__(self, inner):
            super().__init__(inner)
            self.owners = []

        def head_object(self, **kwargs):
            self.owners.append(("HeadObject", kwargs.get("ExpectedBucketOwner")))
            return super().head_object(**kwargs)

        def get_object(self, **kwargs):
            self.owners.append(("GetObject", kwargs.get("ExpectedBucketOwner")))
            return super().get_object(**kwargs)

        def copy_object(self, **kwargs):
            self.owners.append(("CopyObject", kwargs.get("ExpectedBucketOwner"),
                                kwargs.get("ExpectedSourceBucketOwner")))
            return super().copy_object(**kwargs)

        def delete_object(self, **kwargs):
            self.owners.append(("DeleteObject", kwargs.get("ExpectedBucketOwner")))
            return super().delete_object(**kwargs)

    fake_s3 = AssertingS3(pin_env.s3)
    pin_env.s3_holder["client"] = fake_s3
    pin_env.shadow_holder["client"] = FakeIotDataClient()
    user, usecase_id = operator
    device_id = register_device(pin_env, usecase_id)
    staging_key = stage_object(pin_env, image_bytes())

    status, body = invoke(pin_env, "POST", device_id, user, sub_path="/static-image/pin",
                          body={"stagingKey": staging_key, "fileName": "sample.png"})
    assert status in (200, 201), body
    assert fake_s3.owners[:3] == [("HeadObject", PORTAL), ("GetObject", PORTAL),
                                  ("CopyObject", PORTAL, PORTAL)]
    for call in fake_s3.owners:
        assert None not in call[1:], f"unguarded Image_Transport call: {call}"

    # The best-effort deletes of the lifecycle core (pin_requests.py) assert
    # the owner too: a second pin supersedes the first and deletes its
    # canonical object.
    staging_key = stage_object(pin_env, image_bytes(color=(0, 255, 0)))
    status, body = invoke(pin_env, "POST", device_id, user, sub_path="/static-image/pin",
                          body={"stagingKey": staging_key, "fileName": "sample2.png"})
    assert status in (200, 201), body
    deletes = [c for c in fake_s3.owners if c[0] == "DeleteObject"]
    assert deletes and all(c[1] == PORTAL for c in deletes)
