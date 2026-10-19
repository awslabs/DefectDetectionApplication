"""``workflow_debug.logs_writer`` (workflow-debug-mode design §6.2 step 6;
tasks.md 3.4): the batching limits, one ``writeTs`` per batch, the ``id`` /
``receivedMs`` / ``writeTs`` stamps, ``stream_name`` with a colon-bearing
thing name, ``ensure_group`` idempotence, the retry/raise path and the inline
recreation of a missing group against a stub (and ``log_group_missing`` when
the recreation fails).
"""
import json

import pytest
from botocore.exceptions import ClientError

from workflow_debug import logs_writer as lw

SESSION = "6f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
INSTANCE = "0b1c2d3e-4f5a-4b6c-8d9e-0f1a2b3c4d5e"
WF = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
GROUP = "/dda/workflow-debug/uc-1"
STREAM = "core-thing/" + WF


def client_error(code, operation="PutLogEvents"):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class StubLogs:
    """Records every call; ``put_failures`` is a queue of exceptions the next
    ``put_log_events`` calls raise before succeeding; ``describe_raises``
    makes ``describe_log_streams`` raise (a missing group); ``create_group_raises``
    makes the recreation fail."""

    def __init__(self):
        self.calls = []
        self.put_failures = []
        self.describe_raises = None
        self.create_group_raises = None
        self.create_group_exists = False
        self.create_stream_exists = False

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))

    def put_log_events(self, **kwargs):
        self._record("put_log_events", kwargs)
        if self.put_failures:
            raise self.put_failures.pop(0)
        return {"nextSequenceToken": "ignored"}

    def describe_log_streams(self, **kwargs):
        self._record("describe_log_streams", kwargs)
        if self.describe_raises is not None:
            raise self.describe_raises
        return {"logStreams": []}

    def create_log_group(self, **kwargs):
        self._record("create_log_group", kwargs)
        if self.create_group_raises is not None:
            raise self.create_group_raises
        if self.create_group_exists:
            raise client_error("ResourceAlreadyExistsException", "CreateLogGroup")
        return {}

    def put_retention_policy(self, **kwargs):
        self._record("put_retention_policy", kwargs)
        return {}

    def tag_resource(self, **kwargs):
        self._record("tag_resource", kwargs)
        return {}

    def create_log_stream(self, **kwargs):
        self._record("create_log_stream", kwargs)
        if self.create_stream_exists:
            raise client_error("ResourceAlreadyExistsException", "CreateLogStream")
        return {}

    def names(self):
        return [name for name, _ in self.calls]

    def puts(self):
        return [kwargs for name, kwargs in self.calls if name == "put_log_events"]


@pytest.fixture(autouse=True)
def fresh_stream_cache():
    lw.forget_streams()
    yield
    lw.forget_streams()


def records(n):
    return [{"type": "log", "ts": 1792075062600 + i, "level": "INFO", "logger": "x", "message": "m%d" % i}
            for i in range(n)]


class TestNames:
    def test_stream_name_maps_colons_to_underscores(self):
        assert lw.stream_name("site:line-3:cam_01", WF) == "site_line-3_cam_01/" + WF
        assert lw.stream_name("core-thing", WF) == "core-thing/" + WF
        assert ":" not in lw.stream_name("a:b:c", WF).split("/")[0]

    def test_log_group_name_and_arn(self, monkeypatch):
        assert lw.log_group_name("uc-1") == "/dda/workflow-debug/uc-1"
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("PORTAL_ACCOUNT_ID", "111111111111")
        assert lw.log_group_arn(GROUP) == "arn:aws:logs:us-east-1:111111111111:log-group:" + GROUP
        assert lw.log_group_arn(GROUP, region="eu-west-1", account="222222222222").startswith(
            "arn:aws:logs:eu-west-1:222222222222:")


class TestEnsureGroup:
    def test_creates_sets_retention_and_tags(self):
        logs = StubLogs()
        lw.ensure_group(logs, GROUP, 14, "uc-1", group_arn="arn:aws:logs:us-east-1:111111111111:log-group:" + GROUP)
        assert logs.names() == ["create_log_group", "put_retention_policy", "tag_resource"]
        assert logs.calls[0][1] == {"logGroupName": GROUP}
        assert logs.calls[1][1] == {"logGroupName": GROUP, "retentionInDays": 14}
        assert logs.calls[2][1] == {"resourceArn": "arn:aws:logs:us-east-1:111111111111:log-group:" + GROUP,
                                    "tags": {"dda-portal:usecase-id": "uc-1"}}

    def test_idempotent_on_an_existing_group(self):
        logs = StubLogs()
        logs.create_group_exists = True
        lw.ensure_group(logs, GROUP, 7, "uc-1", group_arn="arn")
        assert logs.names() == ["create_log_group", "put_retention_policy", "tag_resource"]

    def test_other_errors_propagate(self):
        logs = StubLogs()
        logs.create_group_raises = client_error("AccessDeniedException", "CreateLogGroup")
        with pytest.raises(ClientError):
            lw.ensure_group(logs, GROUP, 7, "uc-1", group_arn="arn")


class TestEnsureStream:
    def test_created_once_per_container_and_existing_is_fine(self):
        logs = StubLogs()
        logs.create_stream_exists = True
        lw.ensure_stream(logs, GROUP, STREAM)
        lw.ensure_stream(logs, GROUP, STREAM)
        assert logs.names() == ["create_log_stream"]
        lw.ensure_stream(logs, GROUP, STREAM, force=True)
        assert logs.names() == ["create_log_stream", "create_log_stream"]


class TestStampsAndBatches:
    def test_stamps(self):
        stamped = lw.stamp_records(records(3), session_id=SESSION, instance=INSTANCE, seq=17,
                                   received_ms=1792075062700, write_ts=1792075063000)
        assert [s["id"] for s in stamped] == ["{0}:{1}:17:{2}".format(SESSION, INSTANCE, i) for i in range(3)]
        assert {s["receivedMs"] for s in stamped} == {1792075062700}
        assert {s["writeTs"] for s in stamped} == {1792075063000}
        assert stamped[0]["message"] == "m0"

    def test_build_events_uses_the_write_time_as_the_timestamp(self):
        stamped = lw.stamp_records(records(2), session_id=SESSION, instance=INSTANCE, seq=1,
                                   received_ms=1, write_ts=1792075063000)
        events, replaced = lw.build_events(stamped)
        assert replaced == 0
        assert [e["timestamp"] for e in events] == [1792075063000, 1792075063000]
        assert json.loads(events[0]["message"])["id"].endswith(":1:0")

    def test_oversized_record_is_replaced_by_a_marker(self):
        big = {"type": "run", "ts": 1, "blob": "x" * (lw.MAX_EVENT_BYTES + 10)}
        stamped = lw.stamp_records([big], session_id=SESSION, instance=INSTANCE, seq=1, received_ms=1, write_ts=2)
        events, replaced = lw.build_events(stamped)
        assert replaced == 1
        marker = json.loads(events[0]["message"])
        assert marker["type"] == "ingest_error" and marker["reason"] == "record_too_large"
        assert marker["id"] == stamped[0]["id"] and marker["writeTs"] == 2 and marker["originalType"] == "run"

    def test_batches_split_at_ten_thousand_events(self):
        events = [{"timestamp": 1, "message": "m"}] * 10_001
        groups = lw.batches(events)
        assert [len(g) for g in groups] == [10_000, 1]

    def test_batches_split_at_one_megabyte_with_the_overhead(self):
        message = "x" * (100 * 1024)
        events = [{"timestamp": 1, "message": message}] * 11
        groups = lw.batches(events)
        for group in groups:
            assert sum(len(e["message"]) + 26 for e in group) <= lw.MAX_BATCH_BYTES
        assert [len(g) for g in groups] == [10, 1]
        assert lw.batches([]) == []


class TestPutRecords:
    def test_full_write(self):
        logs = StubLogs()
        clock = lambda: 1792075063.123  # noqa: E731
        result = lw.put_records(logs, GROUP, STREAM, records(3), 1792075062700, session_id=SESSION,
                                instance=INSTANCE, seq=17, clock=clock, sleep=lambda s: None)
        assert result.events == 3 and result.batches == 1 and result.recreated_group is False
        assert logs.names() == ["create_log_stream", "put_log_events"]
        put = logs.puts()[0]
        assert put["logGroupName"] == GROUP and put["logStreamName"] == STREAM
        messages = [json.loads(e["message"]) for e in put["logEvents"]]
        assert {m["writeTs"] for m in messages} == {1792075063123}
        assert {e["timestamp"] for e in put["logEvents"]} == {1792075063123}
        assert [m["id"] for m in messages] == ["{0}:{1}:17:{2}".format(SESSION, INSTANCE, i) for i in range(3)]
        assert {m["receivedMs"] for m in messages} == {1792075062700}
        assert result.bytes == sum(len(e["message"].encode("utf-8")) for e in put["logEvents"])

    def test_throttling_is_retried_three_times_then_succeeds(self):
        logs = StubLogs()
        logs.put_failures = [client_error("ThrottlingException")] * 3
        sleeps = []
        lw.put_records(logs, GROUP, STREAM, records(1), 1, session_id=SESSION, instance=INSTANCE, seq=1,
                       clock=lambda: 1.0, sleep=sleeps.append)
        assert sleeps == [0.2, 0.5, 1.0]
        assert len(logs.puts()) == 4

    def test_fourth_throttle_raises(self):
        logs = StubLogs()
        logs.put_failures = [client_error("ThrottlingException")] * 4
        sleeps = []
        with pytest.raises(lw.PutThrottled):
            lw.put_records(logs, GROUP, STREAM, records(1), 1, session_id=SESSION, instance=INSTANCE, seq=1,
                           clock=lambda: 1.0, sleep=sleeps.append)
        assert sleeps == [0.2, 0.5, 1.0]
        assert len(logs.puts()) == 4

    def test_missing_group_is_recreated_inline_and_the_batch_written_once_more(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("PORTAL_ACCOUNT_ID", "111111111111")
        logs = StubLogs()
        logs.put_failures = [client_error("ResourceNotFoundException")]
        logs.describe_raises = client_error("ResourceNotFoundException", "DescribeLogStreams")
        result = lw.put_records(logs, GROUP, STREAM, records(2), 1, session_id=SESSION, instance=INSTANCE,
                                seq=1, clock=lambda: 1.0, sleep=lambda s: None, retention_days=9, usecase_id="uc-1")
        assert result.recreated_group is True
        assert logs.names() == ["create_log_stream", "put_log_events", "describe_log_streams",
                                "create_log_group", "put_retention_policy", "tag_resource",
                                "create_log_stream", "put_log_events"]
        assert logs.calls[4][1] == {"logGroupName": GROUP, "retentionInDays": 9}
        assert logs.calls[5][1]["tags"] == {"dda-portal:usecase-id": "uc-1"}
        assert logs.calls[2][1] == {"logGroupName": GROUP, "logStreamNamePrefix": STREAM, "limit": 1}

    def test_missing_stream_only_is_recreated_without_touching_the_group(self):
        logs = StubLogs()
        logs.put_failures = [client_error("ResourceNotFoundException")]
        result = lw.put_records(logs, GROUP, STREAM, records(1), 1, session_id=SESSION, instance=INSTANCE,
                                seq=1, clock=lambda: 1.0, sleep=lambda s: None)
        assert result.recreated_group is False
        assert logs.names() == ["create_log_stream", "put_log_events", "describe_log_streams",
                                "create_log_stream", "put_log_events"]

    def test_failed_recreation_raises_log_group_missing(self):
        logs = StubLogs()
        logs.put_failures = [client_error("ResourceNotFoundException")]
        logs.describe_raises = client_error("ResourceNotFoundException", "DescribeLogStreams")
        logs.create_group_raises = client_error("AccessDeniedException", "CreateLogGroup")
        with pytest.raises(lw.LogGroupMissing):
            lw.put_records(logs, GROUP, STREAM, records(1), 1, session_id=SESSION, instance=INSTANCE,
                           seq=1, clock=lambda: 1.0, sleep=lambda s: None, group_arn="arn")
        assert len(logs.puts()) == 1

    def test_a_second_resource_not_found_after_recreation_propagates(self):
        logs = StubLogs()
        logs.put_failures = [client_error("ResourceNotFoundException"), client_error("ResourceNotFoundException")]
        with pytest.raises(ClientError):
            lw.put_records(logs, GROUP, STREAM, records(1), 1, session_id=SESSION, instance=INSTANCE,
                           seq=1, clock=lambda: 1.0, sleep=lambda s: None)

    def test_data_already_accepted_counts_as_written(self):
        logs = StubLogs()
        logs.put_failures = [client_error("DataAlreadyAcceptedException")]
        result = lw.put_records(logs, GROUP, STREAM, records(1), 1, session_id=SESSION, instance=INSTANCE,
                                seq=1, clock=lambda: 1.0, sleep=lambda s: None)
        assert result.events == 1

    def test_other_errors_propagate(self):
        logs = StubLogs()
        logs.put_failures = [client_error("InvalidParameterException")]
        with pytest.raises(ClientError):
            lw.put_records(logs, GROUP, STREAM, records(1), 1, session_id=SESSION, instance=INSTANCE,
                           seq=1, clock=lambda: 1.0, sleep=lambda s: None)

    def test_many_records_go_in_several_batches_with_one_write_time(self):
        logs = StubLogs()
        big = [{"type": "run", "ts": i, "blob": "x" * (120 * 1024)} for i in range(10)]
        result = lw.put_records(logs, GROUP, STREAM, big, 1, session_id=SESSION, instance=INSTANCE, seq=1,
                                clock=lambda: 5.0, sleep=lambda s: None)
        assert result.batches == 2 and len(logs.puts()) == 2
        stamps = {json.loads(e["message"])["writeTs"] for put in logs.puts() for e in put["logEvents"]}
        assert stamps == {5000}
