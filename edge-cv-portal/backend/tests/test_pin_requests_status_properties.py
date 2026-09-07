"""
Property-based test for Pin_Request confirmation reduction and the
provisioning status view (cloud-static-camera-provisioning task 1.3).

# Feature: cloud-static-camera-provisioning, Property 8: Confirmation reduction and status view

*For any* set of Pin_Request items and any device report: reducing an
``applied`` confirmation against the ``pending`` Pin_Request it
references records the device-reported metadata (width, height, format,
file name) and the confirmation timestamp; reducing a failure report
records the device-reported reason and timestamp; and the status view
built from any item set returns the identifier, Sync_Status, operation
type, and creation timestamp of the Pin_Request with the latest creation
timestamp — excluding ``superseded`` items from the current state while
retaining them in history, reporting device metadata exactly when the
most recent pin-type Pin_Request is ``applied``, including connectivity
as exactly one of ``connected`` or ``disconnected`` while the latest
Pin_Request is ``pending``, presenting the device-reported pinned state
as current even when it disagrees with the recorded outcome, and
returning a no-request response (not an error) for an empty item set.

**Validates: Requirements 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 1.7, 1.10, 5.7, 7.5**

Exercises the pure core directly (reduce_pin_confirmation and
build_status_view take plain dicts — no AWS, no fakes beyond a recording
connectivity provider). Item sets are generated as reachable lifecycle
histories (at most one pending, only as the newest item; superseded
items always older than their superseder — plus the transient
mid-submission state where the newest is superseded), with DynamoDB
Decimal-typed numerics mixed in. Example counts come from the conftest
hypothesis profiles (portal-fast locally; the spec-minimum 100 with
HYPOTHESIS_PROFILE=ci) — never hardcoded here.
"""
import sys
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

import conftest  # noqa: F401  (puts backend/functions on sys.path)

sys.modules.pop("pin_requests", None)
import pin_requests  # noqa: E402

DEVICE_ID = "thing-prop8"
BASE_MS = 1_700_000_000_000

_ops = st.sampled_from([pin_requests.OP_PIN, pin_requests.OP_REMOVE])
_formats = st.sampled_from(["JPEG", "PNG", "BMP"])
_file_names = st.text(min_size=1, max_size=24)
_reasons = st.text(min_size=1, max_size=40)
_raw_connectivity = st.sampled_from(
    ["HEALTHY", "UNHEALTHY", "UNKNOWN", ""])

_metadata = st.fixed_dictionaries({
    "width": st.integers(min_value=1, max_value=8192),
    "height": st.integers(min_value=1, max_value=8192),
    "format": _formats,
    "fileName": _file_names,
})


def _maybe_decimal(value, as_decimal):
    return Decimal(int(value)) if as_decimal else value


@st.composite
def _pin_request_items(draw):
    """A reachable Pin_Request history, oldest first.

    Every non-newest item is terminal; the newest may hold any status
    (a superseded newest models the transient mid-submission state).
    """
    count = draw(st.integers(min_value=0, max_value=6))
    items = []
    for index in range(count):
        op = draw(_ops)
        if index == count - 1:
            status = draw(st.sampled_from([
                pin_requests.STATUS_PENDING, pin_requests.STATUS_APPLIED,
                pin_requests.STATUS_FAILED, pin_requests.STATUS_SUPERSEDED,
            ]))
        else:
            status = draw(st.sampled_from(pin_requests.TERMINAL_STATUSES))
        created_at = BASE_MS + index * 1_000
        as_decimal = draw(st.booleans())
        pin_request_id = f"{created_at:014d}#{index:08d}"
        item = {
            "device_id": DEVICE_ID,
            "sk": pin_requests.pin_request_sk(pin_request_id),
            "pin_request_id": pin_request_id,
            "usecase_id": "uc-prop8",
            "op": op,
            "status": status,
            "created_at": _maybe_decimal(created_at, as_decimal),
        }
        if op == pin_requests.OP_PIN:
            item.update({
                "s3_bucket": "dda-component-test",
                "s3_key": f"static-image-pins/{DEVICE_ID}/{pin_request_id}",
                "sha256": "ab" * 32,
                "size_bytes": _maybe_decimal(4096, as_decimal),
                "format": draw(_formats),
                "file_name": draw(_file_names),
            })
        if status == pin_requests.STATUS_APPLIED and op == pin_requests.OP_PIN:
            item["device_metadata"] = {
                key: _maybe_decimal(value, as_decimal)
                if isinstance(value, int) else value
                for key, value in draw(_metadata).items()
            }
        if status == pin_requests.STATUS_FAILED:
            item["failure_reason"] = draw(_reasons)
        if status in pin_requests.TERMINAL_STATUSES:
            item["completed_at"] = _maybe_decimal(
                created_at + 500, as_decimal)
        items.append(item)
    return items


@st.composite
def _scenarios(draw):
    items = draw(_pin_request_items())
    scenario = {"items": items}
    # A device report targeting an issued request (index) or an unknown
    # requestId (index == len(items)).
    if draw(st.booleans()):
        scenario["report"] = {
            "status": draw(st.sampled_from([pin_requests.STATUS_APPLIED,
                                            pin_requests.STATUS_FAILED])),
            "target": draw(st.integers(min_value=0, max_value=len(items))),
            "with_completed_at": draw(st.booleans()),
            "metadata": draw(_metadata),
            "reason": draw(_reasons),
        }
    scenario["camera_entry"] = draw(st.one_of(
        st.none(),
        st.fixed_dictionaries(
            {"absent": st.booleans()},
            optional={"absent_since": st.integers(
                min_value=BASE_MS, max_value=BASE_MS + 10_000)},
        ),
    ))
    scenario["raw_connectivity"] = draw(_raw_connectivity)
    scenario["order"] = draw(st.permutations(list(range(len(items)))))
    return scenario


def _apply_outcome(item, outcome):
    """Persist a PinOutcome onto the item dict, as the table write would."""
    item["status"] = outcome.to_status
    item["completed_at"] = outcome.completed_at
    if outcome.device_metadata is not None:
        item["device_metadata"] = outcome.device_metadata
    if outcome.failure_reason is not None:
        item["failure_reason"] = outcome.failure_reason


# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_scenarios())
def test_confirmation_reduction_and_status_view(scenario):
    """Property 8: applied/failed reduction records metadata/reason and
    the confirmation timestamp; the status view reports the latest
    non-superseded request, retains superseded in history, gates
    deviceMetadata and connectivity exactly, presents the device-reported
    state as current, and answers the empty set without an error."""
    items = scenario["items"]
    now_ms = BASE_MS + 100_000

    # ------------------------------------------------------ reduction
    report = scenario.get("report")
    if report is not None:
        target = report["target"]
        item = items[target] if target < len(items) else None
        reported = {
            "requestId": item["pin_request_id"] if item is not None
            else "unknown-request",
            "status": report["status"],
        }
        if report["with_completed_at"]:
            reported["completedAtEpochMs"] = now_ms - 250
        if report["status"] == pin_requests.STATUS_APPLIED:
            reported["metadata"] = dict(report["metadata"])
        else:
            reported["reason"] = report["reason"]

        outcome = pin_requests.reduce_pin_confirmation(
            item, reported, now_ms)

        if item is not None and \
                item["status"] == pin_requests.STATUS_PENDING:
            # A confirmation/failure report against the pending item it
            # references transitions it, recording the device-reported
            # metadata or reason plus the timestamp (Reqs 4.2, 4.3).
            assert outcome.action == pin_requests.ACTION_TRANSITION
            assert outcome.to_status == report["status"]
            expected_ts = (now_ms - 250 if report["with_completed_at"]
                           else now_ms)
            assert outcome.completed_at == expected_ts
            if report["status"] == pin_requests.STATUS_APPLIED:
                assert outcome.device_metadata == report["metadata"]
                assert outcome.failure_reason is None
            else:
                assert outcome.failure_reason == report["reason"]
                assert outcome.device_metadata is None
            _apply_outcome(item, outcome)
            # Idempotent under duplicate delivery: re-reducing the
            # already-terminal item is a no-op (Reqs 4.1, 4.8).
            replay = pin_requests.reduce_pin_confirmation(
                item, reported, now_ms + 1)
            assert replay.action == pin_requests.ACTION_NOOP
        else:
            # Unknown requestId or non-pending item: no-op (4.8, 5.6).
            assert outcome.action == pin_requests.ACTION_NOOP

    # ---------------------------------------------------------- view
    connectivity_calls = []

    def connectivity_provider():
        connectivity_calls.append(True)
        return scenario["raw_connectivity"]

    shuffled = [items[index] for index in scenario["order"]]
    view = pin_requests.build_status_view(
        DEVICE_ID, shuffled,
        camera_entry=scenario["camera_entry"],
        connectivity_provider=connectivity_provider,
    )

    assert view["deviceId"] == DEVICE_ID

    # Empty item set: a no-request response, not an error (1.10, 4.7).
    assert view["noPinRequest"] == (len(items) == 0)

    # latest = the request with the latest creation timestamp, with
    # superseded items excluded from the current state (4.4, 5.7).
    newest_first = sorted(items, key=lambda i: i["pin_request_id"],
                          reverse=True)
    expected_latest = next(
        (i for i in newest_first
         if i["status"] != pin_requests.STATUS_SUPERSEDED), None)
    if expected_latest is None:
        assert view["latest"] is None
    else:
        latest = view["latest"]
        assert latest["pinRequestId"] == expected_latest["pin_request_id"]
        assert latest["status"] == expected_latest["status"]
        assert latest["op"] == expected_latest["op"]
        assert latest["createdAt"] == int(expected_latest["created_at"])
        if expected_latest["status"] == pin_requests.STATUS_FAILED:
            assert latest["failureReason"] == \
                expected_latest["failure_reason"]
        if expected_latest.get("completed_at") is not None:
            assert latest["completedAt"] == \
                int(expected_latest["completed_at"])

    # Superseded requests are retained in history — as is every request
    # (id, op, creation timestamp, status; newest first) (5.7).
    assert [entry["pinRequestId"] for entry in view["history"]] == \
        [i["pin_request_id"] for i in newest_first]
    for entry, item in zip(view["history"], newest_first):
        assert entry["op"] == item["op"]
        assert entry["status"] == item["status"]
        assert entry["createdAt"] == int(item["created_at"])

    # deviceMetadata exactly when the most recent pin-type request is
    # applied (1.7).
    latest_pin = next((i for i in newest_first
                       if i["op"] == pin_requests.OP_PIN), None)
    if latest_pin is not None and \
            latest_pin["status"] == pin_requests.STATUS_APPLIED:
        assert view["deviceMetadata"] == {
            key: int(value) if isinstance(value, Decimal) else value
            for key, value in latest_pin["device_metadata"].items()
        }
    else:
        assert "deviceMetadata" not in view

    # Connectivity exactly while the latest request is pending, as
    # exactly one of connected / disconnected (4.5).
    if expected_latest is not None and \
            expected_latest["status"] == pin_requests.STATUS_PENDING:
        assert connectivity_calls == [True]
        expected = (pin_requests.CONNECTIVITY_CONNECTED
                    if scenario["raw_connectivity"] == "HEALTHY"
                    else pin_requests.CONNECTIVITY_DISCONNECTED)
        assert view["connectivity"] == expected
    else:
        assert "connectivity" not in view
        assert connectivity_calls == []

    # The device-reported pinned state is presented as the current state
    # even when it disagrees with the recorded outcome (4.6, 4.8, 7.5) —
    # it reflects only the CAMERA#static-image-camera registry entry.
    camera_entry = scenario["camera_entry"]
    if camera_entry is None:
        assert view["deviceReported"] is None
    else:
        reported_state = view["deviceReported"]
        assert reported_state["present"] == (not camera_entry["absent"])
        assert reported_state["absent"] == camera_entry["absent"]
        if "absent_since" in camera_entry:
            assert reported_state["absentSince"] == \
                camera_entry["absent_since"]
        else:
            assert "absentSince" not in reported_state
