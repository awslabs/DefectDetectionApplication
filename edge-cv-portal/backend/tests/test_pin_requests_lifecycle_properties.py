"""
Property-based test for the Pin_Request lifecycle and supersede
(cloud-static-camera-provisioning task 1.2).

# Feature: cloud-static-camera-provisioning, Property 7: Pin_Request lifecycle and supersede

*For any* sequence of submissions, device confirmations, device failure
reports, and supersede events applied to a device's Pin_Requests:
(a) at most one Pin_Request is in the ``pending`` Sync_Status at any
point; (b) every Pin_Request transitions out of ``pending`` at most
once, only to ``applied``, ``failed``, or ``superseded``, and never
transitions again — confirmations or failure reports referencing a
non-pending Pin_Request change nothing; (c) after each submission the
Sync_Channel desired slot equals exactly the newest Pin_Request's
document; (d) the Image_Transport object of a pin-type Pin_Request is
deleted only when that Pin_Request leaves ``pending``, and no transition
ever occurs without a triggering event (no time-based expiry).

**Validates: Requirements 4.1, 5.1, 5.3, 5.6, 2.6**

Runs the real pin_requests helpers against a moto DynamoDB table with a
recording fake S3 client and a recorded fake shadow slot (the submission
flow modeled here — supersede, insert, desired-slot replace — is exactly
the route's, task 2.1). Supersede events occur where the design places
them: inside every submission. Example counts come from the conftest
hypothesis profiles (portal-fast locally; the spec-minimum 100 with
HYPOTHESIS_PROFILE=ci) — never hardcoded here.
"""
import itertools
import sys
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

PIN_TABLE_NAME = "test-pin-requests-lifecycle-props"
BUCKET = "dda-component-test"
USECASE_ID = "uc-pin-lifecycle-props"

_device_counter = itertools.count()


@pytest.fixture(scope="module")
def pin_env(aws_stack):
    """A camera-registry-shaped moto table plus a fresh pin_requests."""
    import boto3

    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=PIN_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "device_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "device_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    sys.modules.pop("pin_requests", None)
    import pin_requests

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=pin_requests,
        table=resource.Table(PIN_TABLE_NAME),
    )


class RecordingS3:
    """Records delete_object calls (the only Image_Transport effect the
    lifecycle core performs)."""

    def __init__(self):
        self.deletes = []

    def delete_object(self, Bucket, Key):
        self.deletes.append((Bucket, Key))
        return {}


# ---------------------------------------------------------------------------
# Event generator
#
# submit(op): the portal submission flow — supersede any pending request,
#     write the new pending item, replace the desired slot wholesale.
# confirm(status, k): a device confirmation/failure report; k selects a
#     target at runtime — an issued request (k mod len) or, when k lands
#     on len(issued), an unknown requestId.
# ---------------------------------------------------------------------------

_events = st.lists(
    st.one_of(
        st.tuples(st.just("submit"), st.sampled_from(["pin", "remove"])),
        st.tuples(st.just("confirm"),
                  st.sampled_from(["applied", "failed"]),
                  st.integers(min_value=0, max_value=63)),
    ),
    min_size=1,
    max_size=12,
)


def _snapshot(module, table, device_id):
    """pin_request_id -> status for every item of the device."""
    return {item["pin_request_id"]: item["status"]
            for item in module.query_pin_request_items(table, device_id)}


def _submit(module, table, device_id, op, now_ms, s3, slot):
    """The portal submission flow the routes implement (task 2.1)."""
    module.supersede_pending_requests(table, device_id, now_ms,
                                      s3_client=s3)
    pin_request_id = module.new_pin_request_id(now_ms)
    kwargs = {}
    if op == module.OP_PIN:
        kwargs = {
            "s3_bucket": BUCKET,
            "s3_key": f"static-image-pins/{device_id}/{pin_request_id}",
            "sha256": "ab" * 32,
            "size_bytes": 4096,
            "image_format": "JPEG",
            "file_name": "sample.jpg",
        }
    item = module.build_pin_request_item(
        device_id, USECASE_ID, op, now_ms,
        pin_request_id=pin_request_id, **kwargs)
    module.insert_pin_request(table, item)
    slot["desired"] = module.build_desired_document(item)
    return item


# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_events)
def test_pin_request_lifecycle_and_supersede(pin_env, events):
    """Property 7: single-pending invariant, single event-triggered
    transition out of pending, newest-request desired slot, and
    leave-pending-only canonical deletes."""
    module, table = pin_env.module, pin_env.table
    device_id = f"thing-prop7-{next(_device_counter)}"
    s3 = RecordingS3()
    slot = {"desired": None}

    issued = []          # pin_request_ids in creation order
    items_by_id = {}     # pin_request_id -> item as written
    terminal_seen = {}   # pin_request_id -> first observed terminal status
    now_ms = 1_700_000_000_000

    for event in events:
        now_ms += 1_000
        before = _snapshot(module, table, device_id)

        if event[0] == "submit":
            op = event[1]
            item = _submit(module, table, device_id, op, now_ms, s3, slot)
            issued.append(item["pin_request_id"])
            items_by_id[item["pin_request_id"]] = item
            expected_changes = {
                rid: module.STATUS_SUPERSEDED
                for rid, status in before.items()
                if status == module.STATUS_PENDING
            }
            expected_changes[item["pin_request_id"]] = module.STATUS_PENDING
        else:
            _, reported_status, k = event
            index = k % (len(issued) + 1)
            if index == len(issued):
                request_id = f"unknown-{k}"
            else:
                request_id = issued[index]
            reported = {
                "requestId": request_id,
                "status": reported_status,
                "completedAtEpochMs": now_ms,
            }
            if reported_status == "applied":
                reported["metadata"] = {"width": 64, "height": 48,
                                        "format": "JPEG",
                                        "fileName": "sample.jpg"}
            else:
                reported["reason"] = "simulated device failure"
            module.apply_pin_confirmation(table, device_id, reported,
                                          now_ms=now_ms, s3_client=s3)
            if before.get(request_id) == module.STATUS_PENDING:
                expected_changes = {request_id: reported_status}
            else:
                # (b): a report referencing an unknown or non-pending
                # request changes nothing.
                expected_changes = {}

        after = _snapshot(module, table, device_id)

        # (d, second half): no transition without a triggering event —
        # the post-event state differs from the pre-event state exactly
        # by what this event implies, nothing else ever changes.
        assert after == {**before, **expected_changes}

        # (a): at most one pending at any point.
        pending = [rid for rid, status in after.items()
                   if status == module.STATUS_PENDING]
        assert len(pending) <= 1

        # (b): a request leaves pending at most once, only to a terminal
        # status, and never transitions again.
        for rid, status in after.items():
            assert status in (module.STATUS_PENDING,) \
                + module.TERMINAL_STATUSES
            if rid in terminal_seen:
                assert status == terminal_seen[rid], \
                    "a terminal Pin_Request transitioned again"
            elif status != module.STATUS_PENDING:
                terminal_seen[rid] = status

        # (c): after each submission the desired slot equals exactly the
        # newest request's document.
        if event[0] == "submit":
            newest = module.query_pin_request_items(table, device_id,
                                                    limit=1)[0]
            assert newest["pin_request_id"] == issued[-1]
            assert slot["desired"] == module.build_desired_document(newest)

        # (d): canonical objects are deleted exactly when a pin-type
        # request leaves pending — never earlier (2.6: retrievable while
        # pending), never for removal requests, never twice.
        expected_deletes = sorted(
            (items_by_id[rid]["s3_bucket"], items_by_id[rid]["s3_key"])
            for rid, status in after.items()
            if status != module.STATUS_PENDING
            and items_by_id[rid]["op"] == module.OP_PIN
        )
        assert sorted(s3.deletes) == expected_deletes
        assert len(s3.deletes) == len(set(s3.deletes))
