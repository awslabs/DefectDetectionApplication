"""
Property-based test for the registry's removal of pending deletes
(rtsp-rtmp-stream-cameras task 29.5; finding 21 (c)).

**Feature: rtsp-rtmp-stream-cameras, Property 30: Deletes of cameras the device never created, and the registry's removal of pending deletes**

The registry clauses of Property 30 (its device clauses are in
test/backend-test/camera_sync/test_property_f2122_never_created_delete.py):

- Reducing the merged shadow after each report, where a null deletes the
  key, the registry SHALL remove any entry pending a Portal delete whose
  id is neither a ``cfg-`` id nor a discovery-managed id of Requirement
  5.11, whatever its type, at the first reduction after the delete is
  marked pending whose state does not report the id in ``cameras`` and
  does not report this delete as failed. A failure that carries no
  ``portalChangeId`` counts as reporting this delete as failed. A failure
  that an earlier change left for the entry SHALL NOT keep it, including
  when the null write is lost.
- A reported failure SHALL keep every entry that is not pending a delete,
  and every ``cfg-`` or discovery-managed entry even when it is pending a
  delete, exactly as before, including a failed create that is pending an
  update.

**Validates: Requirements 5.11, 5.12**

Generators: entries of every id kind (``portal-<hex>``, ``cam-N``,
``cfg-N``, ``disc-N``, ``arv-N``, both static ids, and the near misses
``cfg``, ``CFG-N`` and ``static-image-camera-2``) and type (``RTSP``,
``RTMP``, ``Camera``), most of them pending a Portal delete, the rest
pending an update, synced or failed; for each, a failure an earlier change
left in the shadow (with a ``portalChangeId``, without one, or none); and
the device build that answers the deletes (a task 29 build, which
acknowledges a delete of an unlisted id and nulls the failure key it
knows of, or an older build, which refuses it as ``discovery-managed``),
with a run that loses the null write.

Each example starts from a merged shadow holding the earlier failures,
reduces it as the Portal's own documents event, then merges the device's
report into it (a null deletes the key) and reduces the merged state
again, all with the real ``camera_sync._process_report`` over moto
DynamoDB. After each reduction every entry is compared with a reference
model that states the base rule and Requirement 5.12's exception.
"""
import copy
import itertools
import os
import sys
from types import SimpleNamespace

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from conftest import REGION

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-f2122-p30"
REASON_404 = "Image source not found"
REASON_REFUSED = "discovery-managed"

_device_counter = itertools.count()
_clock = itertools.count(1_730_000_000_000, 1_000)


@pytest.fixture(scope="module")
def sync_env(aws_stack):
    import boto3

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=CAMERA_REGISTRY_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "device_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "device_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-index",
            "KeySchema": [{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    import camera_sync

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(module=camera_sync,
                          registry=resource.Table(CAMERA_REGISTRY_TABLE_NAME))


@pytest.fixture(autouse=True)
def registry_table(sync_env, monkeypatch):
    """The reducer reads its table from the environment at call time."""
    monkeypatch.setenv("CAMERA_REGISTRY_TABLE", CAMERA_REGISTRY_TABLE_NAME)


# ---------------------------------------------------------------------------
# The merged shadow (AWS merge semantics: nested maps merge, null deletes)
# ---------------------------------------------------------------------------

def merge(target, patch):
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict):
            node = target.get(key)
            if not isinstance(node, dict):
                node = {}
                target[key] = node
            merge(node, value)
        else:
            target[key] = value


# ---------------------------------------------------------------------------
# The reference model: the base rule, and Requirement 5.12's exception
# ---------------------------------------------------------------------------

def listed(csid):
    """Requirement 5.11's ids: cfg-, disc- and arv- prefixes, and the two
    static ids, matched with startswith and ==."""
    return (csid.startswith(("cfg-", "disc-", "arv-"))
            or csid in ("static-image-camera", "static-video-camera"))


def pending_op(entry):
    if entry["sync_status"] != "pending":
        return None
    return (entry.get("pending_content") or {}).get("op")


def model_reduce(entry, csid, failure):
    """One reduction of an entry the state does not report in cameras.
    Returns the entry afterwards, None when removed."""
    if failure is not None:
        if failure.get("portalChangeId") == entry.get("portal_change_id"):
            entry = dict(entry, sync_status="failed",
                         failure_reason=failure.get("reason"))
            return entry
        change_id = failure.get("portalChangeId")
        exception = (not listed(csid) and pending_op(entry) == "delete"
                     and change_id is not None)
        if not exception:
            return entry                        # pinned, as before
    if pending_op(entry) == "create":
        return entry                            # delivery lag, as before
    if csid in ("static-image-camera", "static-video-camera"):
        return dict(entry, absent=True)         # absence-tracked, as before
    return None


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

ID_KINDS = ("portal", "portal", "cam", "cfg", "disc", "arv",
            "static-image-camera", "static-video-camera", "cfg",
            "CFG", "static-image-camera-2")


def concrete_id(kind, n):
    return {
        "portal": f"portal-{n:012x}", "cam": f"cam-{n}", "cfg": f"cfg-{n}",
        "disc": f"disc-{n:012x}", "arv": f"arv-{n:012x}", "CFG": f"CFG-{n}",
    }.get(kind, kind)


@st.composite
def entry_specs(draw, n):
    kind = draw(st.sampled_from(ID_KINDS))
    if kind == "cfg" and draw(st.booleans()):
        csid = "cfg"            # the near miss: no '-'
    else:
        csid = concrete_id(kind, n)
    return {
        "csid": csid,
        "type": draw(st.sampled_from(("RTSP", "RTMP", "Camera"))),
        "state": draw(st.sampled_from(("pending_delete", "pending_delete",
                                       "pending_delete", "pending_update",
                                       "synced", "failed"))),
        "earlier": draw(st.sampled_from(("with_change_id", "with_change_id",
                                         "without_change_id", "none"))),
    }


@st.composite
def scenarios(draw):
    count = draw(st.integers(min_value=1, max_value=5))
    specs = []
    for n in range(count):
        specs.append(draw(entry_specs(n + 1)))
    return {
        "entries": list({spec["csid"]: spec for spec in specs}.values()),
        "device": draw(st.sampled_from(("task29", "task29", "older"))),
        "drop_null": draw(st.booleans()),
    }


def build_entry(spec, device_id):
    csid = spec["csid"]
    params = ({"url": "rtsp://10.0.0.1/live"} if spec["type"] == "RTSP"
              else {"url": "rtmp://10.0.0.1/live"} if spec["type"] == "RTMP"
              else {"devicePath": "/dev/video0"})
    item = {
        "device_id": device_id, "sk": f"CAMERA#{csid}",
        "camera_source_id": csid, "usecase_id": "uc-p30",
        "name": f"camera {csid}", "type": spec["type"], "params": params,
        "capabilities": {}, "origin": "portal-created", "version": 0,
        "absent": False,
    }
    if spec["state"] == "pending_delete":
        item.update(sync_status="pending", portal_change_id=f"pc-del-{csid}",
                    pending_content={"op": "delete"})
    elif spec["state"] == "pending_update":
        item.update(sync_status="pending", portal_change_id=f"pc-upd-{csid}",
                    pending_content={"op": "update", "name": "edited",
                                     "type": spec["type"], "params": params})
    elif spec["state"] == "failed":
        item.update(sync_status="failed", portal_change_id=f"pc-old-{csid}",
                    failure_reason="the create failed",
                    pending_content={"op": "create", "name": item["name"],
                                     "type": spec["type"], "params": params})
    else:
        item.update(sync_status="synced", origin="edge-configured",
                    version=2)
    return item


def device_answer(spec, entry, device):
    """The device's report for one entry: (failure or None, null the key)."""
    if pending_op(entry) != "delete":
        return None, False
    csid = spec["csid"]
    change_id = entry["portal_change_id"]
    if csid.startswith("cfg-"):
        return {"reason": REASON_404, "portalChangeId": change_id}, False
    if listed(csid) or device == "older":
        return {"reason": REASON_REFUSED, "portalChangeId": change_id}, False
    # A task 29 device acknowledges the delete, and nulls the failure key
    # it knows the shadow holds (component 12).
    return None, True


def stored(sync_env, device_id):
    items = sync_env.registry.query(
        KeyConditionExpression="device_id = :d",
        ExpressionAttributeValues={":d": device_id}).get("Items", [])
    return {item["sk"][len("CAMERA#"):]: item for item in items
            if item["sk"].startswith("CAMERA#")}


def assert_matches(model, actual, where):
    for csid, expected in model.items():
        item = actual.get(csid)
        if expected is None:
            assert item is None, f"{where}: {csid} was kept: {item}"
            continue
        assert item is not None, f"{where}: {csid} was removed"
        assert item["sync_status"] == expected["sync_status"], (where, csid)
        assert item.get("failure_reason") == expected.get(
            "failure_reason"), (where, csid)
        assert bool(item.get("absent")) == bool(expected.get("absent")), \
            (where, csid)


# ---------------------------------------------------------------------------
# Property 30, registry clauses
# ---------------------------------------------------------------------------

# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(scenarios())
def test_pending_deletes_are_removed_by_requirement_5_12(sync_env, case):
    device_id = f"thing-p30-{next(_device_counter)}"
    specs = {spec["csid"]: spec for spec in case["entries"]}
    shadow = {"cameras": {}, "failures": {}}
    model = {}
    for csid, spec in specs.items():
        entry = build_entry(spec, device_id)
        sync_env.registry.put_item(Item=entry)
        model[csid] = entry
        if spec["earlier"] == "with_change_id":
            shadow["failures"][csid] = {"reason": "an earlier change failed",
                                        "portalChangeId": f"pc-earlier-{csid}"}
        elif spec["earlier"] == "without_change_id":
            shadow["failures"][csid] = {"reason": "applied at the station"}

    def reduce_merged(where):
        reported = copy.deepcopy(shadow)
        reported.update(schemaVersion=1, reportedAt=next(_clock))
        sync_env.module._process_report(device_id, reported,
                                        usecase_id="uc-p30")
        for csid in list(model):
            if model[csid] is not None:
                model[csid] = model_reduce(model[csid], csid,
                                           shadow["failures"].get(csid))
        assert_matches(model, stored(sync_env, device_id), where)

    # The Portal's own documents event: the merged state as it is.
    before = {csid: dict(entry) for csid, entry in model.items()}
    reduce_merged("the Portal's own documents event")

    # The device's report for the deletes, merged into the shadow.
    patch = {"failures": {}}
    for csid, spec in specs.items():
        failure, null_key = device_answer(spec, before[csid], case["device"])
        if failure is not None:
            patch["failures"][csid] = failure
        elif null_key and csid in shadow["failures"]:
            if case["drop_null"]:
                event("the device's null write was lost")
            else:
                patch["failures"][csid] = None
    merge(shadow, patch)
    reduce_merged("the device's report")

    for csid, spec in specs.items():
        removed = model[csid] is None
        kind = ("unlisted" if not listed(csid) else "listed")
        event(f"{kind} {spec['state']}, earlier failure "
              f"{spec['earlier']}: {'removed' if removed else 'kept'}")
        if (not listed(csid) and spec["state"] == "pending_delete"
                and spec["earlier"] != "without_change_id"):
            # 5.12: the first reduction after the delete (the Portal's own
            # event) removes it, on any build, whatever the earlier
            # failure, null write lost or not.
            assert removed, csid
        if listed(csid) and spec["earlier"] != "none":
            # A cfg- or discovery-managed entry stays pinned by its
            # failure, pending a delete or not.
            assert not removed, csid
        if spec["state"] in ("pending_update", "synced", "failed") \
                and spec["earlier"] != "none":
            assert not removed, csid
