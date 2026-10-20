"""
Property test for the Registration_Report remedy
(portal-authorization-and-iam-scoping, task 5.5; design I5 test strategy,
Requirement 7.11).

The oracle restates Requirement 7.11 from the inputs and never reads the
implementation's remedy texts: the remedy is None iff the bucket is
reachable; otherwise it names the earliest unmet step — tag the bucket
(`aws s3control tag-resource ... --tags Key=dda-portal:managed,Value=true`,
whenever it is not tagged), enable ABAC on a portal-account bucket (`aws s3api
put-bucket-abac --bucket <bucket> --abac-status Status=Enabled`), ask the
bucket's account administrator for any other bucket, and under
'tag-objects-only' redeploy the portal to browse it; a bucket with every step
met that is still unreachable gets the enable command. The same property is
checked through `_probe`, the one call the wizard path makes, with a fake
client that answers or refuses.

Validates: Requirements 7.11
"""
import pytest
from botocore.exceptions import ClientError
from hypothesis import given
from hypothesis import strategies as st

import shared_utils as su

PORTAL = "111111111111"
FIXTURE_ACCOUNTS = ("111111111111", "222222222222", "333333333333", "444444444444")
PLACEHOLDER_ACCOUNT = "<bucket owner account>"

BUCKETS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=40).map(
    lambda middle: f"b{middle}0")
ABAC = st.sampled_from([True, False, "unknown"])
KINDS = st.sampled_from(["portal", "cross_account"])
SCOPINGS = st.sampled_from(["tag", "tag-objects-only", "allowlist"])
ACCOUNTS = st.one_of(st.none(), st.sampled_from(FIXTURE_ACCOUNTS))


def earliest_unmet_step(tagged, abac_enabled, account_kind, scoping):
    """Requirement 7.11's order: tag, enable, the account administrator,
    redeploy; every step met yet unreachable -> enable."""
    steps = (
        ("tag", not tagged),
        ("enable", account_kind == "portal" and abac_enabled is not True),
        ("administrator", account_kind != "portal"),
        ("redeploy", account_kind == "portal" and scoping == "tag-objects-only"),
    )
    for step, unmet in steps:
        if unmet:
            return step
    return "enable"


def assert_names_step(remedy, step, bucket, account):
    enable_command = f"aws s3api put-bucket-abac --bucket {bucket} --abac-status Status=Enabled"
    assert isinstance(remedy, str) and remedy
    # The absence checks read the text with the generated name masked, so a
    # name that happens to spell a step's words cannot decide them.
    masked = remedy.replace(bucket, "BUCKET")
    if step == "tag":
        assert (f"aws s3control tag-resource --account-id {account} "
                f"--resource-arn arn:aws:s3:::{bucket} "
                "--tags Key=dda-portal:managed,Value=true") in remedy
        assert "put-bucket-abac" not in masked
    elif step == "enable":
        assert enable_command in remedy
        assert "administrator" not in masked and "redeploy" not in masked
    elif step == "administrator":
        assert "account administrator" in masked and enable_command in remedy
    else:
        assert "redeploy the portal" in masked
        assert "put-bucket-abac" not in masked and "tag-resource" not in masked


def expected_tag_account(account_kind, account_id):
    return account_id or (PORTAL if account_kind == "portal" else PLACEHOLDER_ACCOUNT)


class ProbeClient:
    """HeadBucket plus ListObjectsV2 either answer or are refused."""

    def __init__(self, reachable):
        self.reachable = reachable
        self.calls = []

    def head_bucket(self, **kwargs):
        self.calls.append("head_bucket")
        if not self.reachable:
            raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"},
                               "ResponseMetadata": {"HTTPStatusCode": 403}}, "HeadBucket")
        return {}

    def list_objects_v2(self, **kwargs):
        self.calls.append("list_objects_v2")
        return {"KeyCount": 0}


class TestProperty8RegistrationRemedy:
    """**Feature: portal-authorization-and-iam-scoping, Property 8: The
    registration remedy is null iff the bucket is reachable and otherwise
    names the earliest unmet step** — for any generated (tagged,
    abac_enabled, iam_reachable, account_kind, scoping) tuple, bucket and
    bucket account, `registration_remedy` returns None iff `iam_reachable`,
    else the copy-ready command of the earliest unmet step of Requirement
    7.11 (tag before enable before the account administrator before
    redeploy), the tag command naming the bucket's account, the portal
    account for a portal bucket, or a placeholder."""

    @given(tagged=st.booleans(), abac_enabled=ABAC, iam_reachable=st.booleans(),
           account_kind=KINDS, scoping=SCOPINGS, bucket=BUCKETS, account_id=ACCOUNTS)
    def test_remedy_names_the_earliest_unmet_step(self, tagged, abac_enabled, iam_reachable,
                                                  account_kind, scoping, bucket, account_id):
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("PORTAL_ACCOUNT_ID", PORTAL)
            remedy = su.registration_remedy(tagged, abac_enabled, iam_reachable, account_kind,
                                            scoping, bucket=bucket, account_id=account_id)
        if iam_reachable:
            assert remedy is None
            return
        assert_names_step(remedy, earliest_unmet_step(tagged, abac_enabled, account_kind, scoping),
                          bucket, expected_tag_account(account_kind, account_id))

    @given(tagged=st.booleans(), abac_enabled=ABAC, reachable=st.booleans(),
           client_built=st.booleans(), just_enabled=st.booleans(),
           account_kind=KINDS, scoping=SCOPINGS, bucket=BUCKETS, account_id=ACCOUNTS)
    def test_probe_reports_reachability_with_the_same_remedy(self, tagged, abac_enabled, reachable,
                                                            client_built, just_enabled, account_kind,
                                                            scoping, bucket, account_id):
        client = ProbeClient(reachable) if client_built else None
        sleeps = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("PORTAL_ACCOUNT_ID", PORTAL)
            mp.setattr(su.time, "sleep", lambda seconds: sleeps.append(seconds))
            iam_reachable, remedy = su._probe(
                bucket, s3_client=client, tagged=tagged, abac_enabled=abac_enabled,
                account_kind=account_kind, scoping=scoping, just_enabled=just_enabled,
                account_id=account_id)
        assert iam_reachable is (client_built and reachable)
        # The bounded wait: once, only after a just-enabled bucket's first probe failed.
        assert sleeps == ([su.ATTACH_PROBE_RETRY_DELAY_SECONDS]
                          if client_built and not reachable and just_enabled else [])
        if iam_reachable:
            assert remedy is None
            return
        assert_names_step(remedy, earliest_unmet_step(tagged, abac_enabled, account_kind, scoping),
                          bucket, expected_tag_account(account_kind, account_id))
