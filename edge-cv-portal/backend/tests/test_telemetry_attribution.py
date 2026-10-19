"""``telemetry.attribution`` (workflow-telemetry-dashboards design §6.4;
tasks.md 3.2): the property tests of §6.4 with injected lookups.

- attributed only when the certificate resolves and the Device_Record names
  the topic's use case;
- a retryable lookup error yields neither record nor rejection (it raises);
- a denied lookup never yields a record;
- a ``c`` mismatch is rejected whatever the payload says;
- the 120th Event of a Pair in a minute is accepted and the 121st is
  ``rate_exceeded``;
- a Device without a Device_Record; ``workflow_not_in_usecase``;
  ``usecase_disabled``; the step order of the detail.
"""
import base64
import json
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from edge_telemetry_common.identity import LookupDenied, LookupRetryable
from telemetry import attribution, schemas

REGION = "us-east-1"
ACCOUNT = "222222222222"
UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF = "7a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
INSTANCE = "0f1e2d3c-4b5a-4968-8776-65544332211a"
DEVICE = "mic-730-demo"
PR = "ab" * 32
T0 = 1_792_000_000_000
NOW = T0 + 10_400


def usecase(**over):
    base = {"usecase_id": UC, "account_id": ACCOUNT, "region": REGION,
            "cross_account_role_arn": "arn:aws:iam::{0}:role/DDAPortalAccessRole".format(ACCOUNT)}
    base.update(over)
    return base


def summary_payload(**over):
    message = {
        "schema": "dda.summary.v1", "intervalStart": T0, "intervalEnd": T0 + 10_000, "emittedAt": T0 + 10_100,
        "deviceId": DEVICE, "workflowId": WF, "workflowVersion": "3",
        "producer": {"name": "engine", "version": "1.0.0", "instance": INSTANCE},
        "seq": 7, "intervalS": 10, "idle": False,
        "runs": {"completed": 1, "failed": 0, "dropped": 0},
        "model": {"state": "READY", "models": {}, "waits": 0, "waitedMs": 0},
        "telemetry": {"queueDrops": 0, "publishFailures": 0, "eventsDropped": 0},
    }
    message.update(over)
    return message


def event_payload(**over):
    message = {
        "schema": "dda.event.v1", "type": "run_error", "eventId": str(uuid.uuid4()), "ts": NOW - 100,
        "deviceId": DEVICE, "workflowId": WF, "workflowVersion": "3",
        "producer": {"name": "engine", "version": "1.0.0", "instance": INSTANCE},
        "data": {"error": "boom", "failingNodeId": "n1", "count": 1, "firstTs": NOW - 100, "lastTs": NOW - 100},
    }
    message.update(over)
    return message


def encode(payload):
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


_DEFAULT = object()


def message(payload=None, *, topic=_DEFAULT, pr=PR, c=DEVICE, r=NOW, m=None, kind="summary"):
    if payload is None:
        payload = summary_payload() if kind == "summary" else event_payload()
    if topic is _DEFAULT:
        topic = "dda/telemetry/{0}/{1}/{2}".format(UC, WF, kind)
    return {"p": encode(payload), "t": topic, "pr": pr, "c": c, "r": r, "m": m or str(uuid.uuid4())}


class Fake:
    """Recording lookups with per-step answers."""

    def __init__(self, *, usecases=None, enabled=True, thing=DEVICE, devices=None, workflows=None, resolve=None):
        self.usecases = usecases if usecases is not None else {UC: usecase()}
        self.enabled = enabled
        self.thing = thing
        self.devices = devices if devices is not None else {DEVICE: {"device_id": DEVICE, "usecase_id": UC}}
        self.workflows = workflows if workflows is not None else {WF: {"workflow_id": WF, "usecase_id": UC}}
        self.resolve = resolve
        self.calls = []

    def lookups(self):
        def _usecase(usecase_id):
            self.calls.append(("usecase", usecase_id))
            return self.usecases.get(usecase_id)

        def _enabled(u):
            self.calls.append(("enabled", u["usecase_id"]))
            return self.enabled

        def _resolve(pr, u):
            self.calls.append(("resolve", pr))
            if self.resolve is not None:
                return self.resolve(pr, u)
            return self.thing

        def _device(device_id):
            self.calls.append(("device", device_id))
            return self.devices.get(device_id)

        def _workflow(workflow_id):
            self.calls.append(("workflow", workflow_id))
            return self.workflows.get(workflow_id)

        return attribution.Lookups(_usecase, _enabled, _resolve, _device, _workflow)


def decide(msg, fake=None, **kwargs):
    fake = fake or Fake()
    options = {"account": ACCOUNT, "portal_region": REGION, "now_ms": NOW}
    options.update(kwargs)
    return attribution.decide(msg, fake.lookups(), **options)


# --------------------------------------------------------------------------- the happy path
class TestAccepted:
    def test_a_summary_is_attributed(self):
        decision = decide(message(m="aaaabbbb-0000-4000-8000-00000000000a"))
        assert decision.accepted and decision.reason is None
        record = decision.record
        assert record.usecase_id == UC and record.device_id == DEVICE and record.workflow_id == WF
        assert record.kind == "summary" and record.schema == "dda.summary.v1"
        assert record.producer == "engine" and record.instance == INSTANCE and record.seq == 7
        assert record.received_ms == NOW and record.event_ms == T0 + 10_000 and record.ts_source == "device"
        assert record.message_id == "aaaabbbb-0000-4000-8000-00000000000a"
        assert record.fields["runs"] == {"completed": 1, "failed": 0, "dropped": 0}
        assert record.blocks_dropped == [] and record.dropped == []
        assert json.loads(record.raw_text)["seq"] == 7 and record.usecase["usecase_id"] == UC
        assert decision.usecase_id == UC and decision.account == ACCOUNT

    def test_an_event_is_attributed(self):
        counter = attribution.EventRateCounter()
        decision = decide(message(kind="event"), rate_counter=counter)
        assert decision.accepted and decision.record.kind == "event"
        assert decision.record.event_ms == NOW - 100 and decision.record.ts_source == "device"
        assert decision.record.fields["severity"] == "error"

    def test_a_live_frame_is_attributed_with_the_slot_rule(self):
        payload = {"schema": "dda.live.v1", "ts": NOW, "instance": INSTANCE, "seq": 33, "deviceId": DEVICE,
                   "workflowId": WF,
                   "frame": {"bucket": "dda-inference-results-222222222222",
                             "key": schemas.live_key_for(UC, DEVICE, WF, 33), "width": 640, "height": 360, "bytes": 1000}}
        decision = decide(message(payload, kind="live"))
        assert decision.accepted and decision.record.kind == "live" and decision.record.seq == 33
        assert decision.record.instance == INSTANCE and decision.record.producer == "live"
        payload["frame"]["key"] = schemas.live_key_for(str(uuid.uuid4()), DEVICE, WF, 33)
        refused = decide(message(payload, kind="live"))
        assert refused.reason == "schema_invalid"

    def test_unowned_blocks_are_dropped_but_the_summary_stands(self):
        payload = summary_payload(producer={"name": "ppe-clipper", "version": "1", "instance": INSTANCE},
                                  ext={"ppe": {"people": {"seen": 2, "compliant": 1, "violating": 1},
                                               "violations": {"frames": 1, "byReason": {}}, "clipsOpened": 0, "frames": 5}})
        decision = decide(message(payload))
        assert decision.accepted
        assert sorted(decision.record.blocks_dropped) == ["model", "runs"]
        assert "runs" not in decision.record.fields and decision.record.fields["ext"]["ppe"]["frames"] == 5
        engine = decide(message(summary_payload(ext={"ppe": {"people": {"seen": 2, "compliant": 1, "violating": 1},
                                                            "violations": {"frames": 1, "byReason": {}},
                                                            "clipsOpened": 0, "frames": 5}})))
        assert engine.record.blocks_dropped == [] and "ppe" in engine.record.fields["ext"]

    def test_the_lookups_are_consulted_in_the_step_order(self):
        fake = Fake()
        decide(message(), fake)
        assert [name for name, _ in fake.calls] == ["usecase", "enabled", "resolve", "device", "workflow"]

    def test_a_summary_far_from_the_receive_time_charts_at_receive_time(self):
        decision = decide(message(summary_payload(intervalStart=T0 - 3_600_000, intervalEnd=T0 - 3_590_000,
                                                  emittedAt=T0 - 3_590_000)))
        assert decision.accepted and decision.record.event_ms == NOW and decision.record.ts_source == "received"


# --------------------------------------------------------------------------- the refusals, step by step
class TestRefusals:
    def test_topic(self):
        assert decide(message(topic="dda/telemetry/{0}/{1}".format(UC, WF))).reason == "attribution_mismatch"
        assert decide(message(topic="dda/telemetry/{0}/{1}/frames".format(UC, WF))).reason == "schema_unknown"
        assert decide(message(topic="dda/debug/{0}/{1}/record".format(UC, WF))).reason == "attribution_mismatch"
        assert decide(message(topic="dda/telemetry/not-a-uuid/{0}/summary".format(WF))).reason == "attribution_mismatch"
        assert decide(message(topic="dda/telemetry/{0}/not-a-uuid/summary".format(UC))).reason == "attribution_mismatch"
        assert decide(message(topic=None)).reason == "attribution_mismatch"
        fake = Fake()
        assert decide(message(topic="dda/results"), fake).reason == "attribution_mismatch"
        assert fake.calls == [], "no lookup before the topic parses"

    def test_use_case(self):
        assert decide(message(), Fake(usecases={})).reason == "attribution_mismatch"
        assert decide(message(), Fake(usecases={UC: usecase(account_id="333333333333")})).reason == "attribution_mismatch"
        assert decide(message(), Fake(usecases={UC: usecase(region="eu-west-1")})).reason == "attribution_mismatch"
        decision = decide(message(), Fake(enabled=False))
        assert decision.reason == "usecase_disabled" and decision.usecase_id == UC

    def test_identity(self):
        assert decide(message(pr="not-a-thumbprint")).reason == "unattributed"
        assert decide(message(pr=None)).reason == "unattributed"
        assert decide(message(), Fake(thing=None)).reason == "unattributed"
        assert decide(message(c="other-thing")).reason == "attribution_mismatch"
        assert decide(message(c=DEVICE + "#3")).accepted, "a #<n> client suffix is stripped"
        assert decide(message(c=None)).reason == "attribution_mismatch"

    def test_a_retryable_lookup_raises_and_yields_nothing(self):
        def retryable(pr, u):
            raise LookupRetryable("ThrottlingException")
        with pytest.raises(LookupRetryable):
            decide(message(), Fake(resolve=retryable))

    def test_a_denied_lookup_is_identity_unavailable(self):
        def denied(pr, u):
            raise LookupDenied("identity lookup denied for account " + ACCOUNT)
        decision = decide(message(), Fake(resolve=denied))
        assert decision.reason == "identity_unavailable" and decision.record is None
        assert decision.usecase_id == UC

    def test_device_record(self):
        assert decide(message(), Fake(devices={})).reason == "attribution_mismatch"
        other = {DEVICE: {"device_id": DEVICE, "usecase_id": str(uuid.uuid4())}}
        assert decide(message(), Fake(devices=other)).reason == "attribution_mismatch"
        unrecorded = {DEVICE: {"device_id": DEVICE}}
        assert decide(message(), Fake(devices=unrecorded)).reason == "attribution_mismatch"

    def test_workflow(self):
        assert decide(message(), Fake(workflows={})).reason == "workflow_not_in_usecase"
        other = {WF: {"workflow_id": WF, "usecase_id": str(uuid.uuid4())}}
        assert decide(message(), Fake(workflows=other)).reason == "workflow_not_in_usecase"

    def test_schema(self):
        bad = message()
        bad["p"] = "!!not base64!!"
        assert decide(bad).reason == "invalid_json"
        assert decide(message(summary_payload(intervalS=120, intervalEnd=T0 + 120_000, emittedAt=T0 + 120_000))).reason == "schema_invalid"
        assert decide(message(event_payload(), kind="summary")).reason == "schema_invalid"
        assert decide(message({"schema": "dda.other.v1"})).reason == "schema_unknown"
        huge = message(summary_payload(vendor="x" * 20_000))
        assert decide(huge).reason == "too_large"
        no_m = message()
        no_m["m"] = ""
        assert decide(no_m).reason == "invalid_json"

    def test_payload_ids_must_match_the_certificate_and_the_topic(self):
        assert decide(message(summary_payload(deviceId="someone-else"))).reason == "attribution_mismatch"
        assert decide(message(summary_payload(workflowId=str(uuid.uuid4())))).reason == "attribution_mismatch"

    def test_a_decision_never_carries_a_payload_value(self):
        secret = "hunter2-" + "x" * 50
        decision = decide(message(summary_payload(deviceId=secret)))
        assert not decision.accepted
        assert secret not in (decision.detail or "") and secret not in (decision.reason or "")


# --------------------------------------------------------------------------- the Event rate (step 7b)
class TestEventRate:
    def test_the_120th_event_is_accepted_and_the_121st_is_rate_exceeded(self):
        counter = attribution.EventRateCounter()
        for index in range(120):
            decision = decide(message(event_payload(), kind="event"), rate_counter=counter, now_ms=NOW + index)
            assert decision.accepted, index
        refused = decide(message(event_payload(), kind="event"), rate_counter=counter, now_ms=NOW + 120)
        assert refused.reason == "rate_exceeded" and refused.usecase_id == UC
        # Another Pair has its own counter.
        other = decide(message(event_payload(deviceId="other-dev"), kind="event", c="other-dev"),
                       Fake(thing="other-dev", devices={"other-dev": {"device_id": "other-dev", "usecase_id": UC}}),
                       rate_counter=counter, now_ms=NOW + 120)
        assert other.accepted
        # The window slides: a minute later the Pair is accepted again.
        later = decide(message(event_payload(), kind="event"), rate_counter=counter, now_ms=NOW + 60_000)
        assert later.accepted

    def test_summaries_are_never_rate_limited(self):
        counter = attribution.EventRateCounter(limit=1)
        for _ in range(3):
            assert decide(message(), rate_counter=counter).accepted

    def test_a_refused_event_does_not_count(self):
        counter = attribution.EventRateCounter(limit=2)
        assert decide(message(event_payload(), kind="event"), rate_counter=counter).accepted
        assert decide(message(event_payload(type="explosion"), kind="event"), rate_counter=counter).reason == "schema_invalid"
        assert decide(message(event_payload(), kind="event"), rate_counter=counter).accepted
        assert decide(message(event_payload(), kind="event"), rate_counter=counter).reason == "rate_exceeded"

    def test_counter_without_injection_means_no_limit(self):
        for _ in range(3):
            assert decide(message(event_payload(), kind="event")).accepted

    def test_counter_api(self):
        counter = attribution.EventRateCounter(limit=2, window_ms=1000)
        assert counter.accept("k", 0) and counter.accept("k", 10) and not counter.accept("k", 20)
        assert counter.count("k") == 2
        assert counter.accept("k", 1000)
        counter.clear()
        assert counter.count("k") == 0


# --------------------------------------------------------------------------- properties
thing_names = st.from_regex(r"^[A-Za-z0-9_-]{1,24}$", fullmatch=True)


@settings(max_examples=60, deadline=None)
@given(resolved=st.one_of(st.none(), thing_names), record_usecase=st.one_of(st.none(), st.just(UC), st.uuids().map(str)),
       client=thing_names)
def test_attributed_only_when_the_certificate_resolves_and_the_record_names_the_use_case(resolved, record_usecase, client):
    devices = {} if record_usecase is None else {resolved or DEVICE: {"device_id": resolved or DEVICE,
                                                                       "usecase_id": record_usecase}}
    fake = Fake(thing=resolved, devices=devices)
    decision = decide(message(summary_payload(deviceId=client), c=client), fake)
    expected = resolved is not None and client == resolved and record_usecase == UC
    assert decision.accepted is expected
    if not expected:
        assert decision.record is None and decision.reason in attribution.REJECTION_REASONS


@settings(max_examples=40, deadline=None)
@given(client=thing_names, payload_device=thing_names)
def test_a_client_id_mismatch_is_rejected_whatever_the_payload_says(client, payload_device):
    if client == DEVICE:
        client = DEVICE + "-x"
    decision = decide(message(summary_payload(deviceId=payload_device), c=client))
    assert not decision.accepted and decision.reason == "attribution_mismatch"


@settings(max_examples=40, deadline=None)
@given(kind=st.sampled_from(["retryable", "denied"]))
def test_a_failed_lookup_never_yields_a_record(kind):
    def failing(pr, u):
        raise LookupRetryable("x") if kind == "retryable" else LookupDenied("y")
    if kind == "retryable":
        with pytest.raises(LookupRetryable):
            decide(message(), Fake(resolve=failing))
    else:
        decision = decide(message(), Fake(resolve=failing))
        assert decision.record is None and decision.reason == "identity_unavailable"


@settings(max_examples=30, deadline=None)
@given(count=st.integers(min_value=1, max_value=130))
def test_the_rate_boundary(count):
    counter = attribution.EventRateCounter()
    outcomes = [counter.accept("pair", NOW + i) for i in range(count)]
    assert outcomes == [i < 120 for i in range(count)]
