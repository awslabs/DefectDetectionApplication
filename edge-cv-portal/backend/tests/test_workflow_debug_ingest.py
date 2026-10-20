"""``workflow_debug_ingest`` (workflow-debug-mode design §6.2; tasks.md 3.5)
against moto (the sessions table with both indexes, the conftest tables) and
stubbed ``logs`` / ``iot`` clients with injected lookups.

A full accept writes the events with ``id`` / ``writeTs`` / ``receivedMs`` in
chronological order; each rejection reason; the dedup protocol across a
``PutLogEvents`` throttle (raise, no ``SEQ#``; a second invocation writes and
is not ``duplicate``; a third is ``duplicate`` and writes nothing); the inline
log-group recreation; ``LookupRetryable`` raises and ``LookupDenied`` is the
terminal ``identity_unavailable``; the logs never carry a payload string.
"""
import base64
import json
import logging
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"
PORTAL_ACCOUNT = "111111111111"
TABLE = "test-workflow-debug-sessions"
PR = "ab" * 32
SECRET_MARKER = "payload-secret-marker-7f3a9c"


def client_error(code, operation="PutLogEvents"):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class StubLogs:
    def __init__(self):
        self.calls = []
        self.put_failures = []
        self.describe_raises = None

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))

    def put_log_events(self, **kwargs):
        self._record("put_log_events", kwargs)
        if self.put_failures:
            raise self.put_failures.pop(0)
        return {}

    def describe_log_streams(self, **kwargs):
        self._record("describe_log_streams", kwargs)
        if self.describe_raises is not None:
            raise self.describe_raises
        return {"logStreams": []}

    def create_log_group(self, **kwargs):
        self._record("create_log_group", kwargs)
        return {}

    def put_retention_policy(self, **kwargs):
        self._record("put_retention_policy", kwargs)
        return {}

    def tag_resource(self, **kwargs):
        self._record("tag_resource", kwargs)
        return {}

    def create_log_stream(self, **kwargs):
        self._record("create_log_stream", kwargs)
        return {}

    def puts(self):
        return [kwargs for name, kwargs in self.calls if name == "put_log_events"]

    def names(self):
        return [name for name, _ in self.calls]


class FakeIot:
    """``ListPrincipalThings`` answered from a thumbprint -> thing map, or an
    exception queued for the next call."""

    def __init__(self, things):
        self.things = things
        self.raise_next = []
        self.calls = []

    def list_principal_things(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_next:
            raise self.raise_next.pop(0)
        thumbprint = kwargs["principal"].rsplit("/", 1)[1]
        thing = self.things.get(thumbprint)
        return {"things": [thing] if thing else []}


def ensure_sessions_table():
    client = boto3.client("dynamodb", region_name=REGION)
    if TABLE in client.list_tables()["TableNames"]:
        return
    client.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "gsi_usecase", "AttributeType": "S"}, {"AttributeName": "gsi_pair", "AttributeType": "S"},
            {"AttributeName": "gsi_sort", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {"IndexName": "by-usecase",
             "KeySchema": [{"AttributeName": "gsi_usecase", "KeyType": "HASH"},
                           {"AttributeName": "gsi_sort", "KeyType": "RANGE"}],
             "Projection": {"ProjectionType": "ALL"}},
            {"IndexName": "by-pair",
             "KeySchema": [{"AttributeName": "gsi_pair", "KeyType": "HASH"},
                           {"AttributeName": "gsi_sort", "KeyType": "RANGE"}],
             "Projection": {"ProjectionType": "ALL"}},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


class Clock:
    def __init__(self, start=1792075062.0):
        self.now = start

    def __call__(self):
        return self.now


class World:
    """One use case, device, workflow and session on the moto stack, plus the
    stubbed clients, with helpers to build rule events."""

    def __init__(self, aws_stack, ingest, logs, iot, clock):
        self.stack = aws_stack
        self.ingest = ingest
        self.logs = logs
        self.iot = iot
        self.clock = clock
        self.usecase_id = "uc-{0}".format(uuid.uuid4())
        self.device = "core-thing-{0}".format(uuid.uuid4().hex[:6])
        self.workflow_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        self.instance = str(uuid.uuid4())
        aws_stack.tables.usecases.put_item(Item={"usecase_id": self.usecase_id, "name": "u",
                                                 "account_id": PORTAL_ACCOUNT, "region": REGION})
        aws_stack.tables.devices.put_item(Item={"device_id": self.device, "usecase_id": self.usecase_id,
                                                "target_architecture": "arm64_jp5"})
        aws_stack.tables.workflows.put_item(Item={"workflow_id": self.workflow_id, "usecase_id": self.usecase_id,
                                                  "created_at": 1, "name": "wf"})
        iot.things[PR] = self.device
        self.sessions_client = boto3.client("dynamodb", region_name=REGION)
        self.table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)

    def create_session(self, *, max_per_second=10, state=None, expires_in_s=3600, session_id=None):
        from workflow_debug import sessions as s
        session_id = session_id or self.session_id
        now_ms = int(self.clock() * 1000)
        actions = s.creation_actions(
            TABLE, session_id=session_id, usecase_id=self.usecase_id, workflow_id=self.workflow_id,
            workflow_version="1", devices=[self.device], all_devices=False, enabled_at=now_ms,
            expires_at=now_ms + expires_in_s * 1000, actor="user-1",
            options={"level": "debug", "images": False, "imageMaxKb": 150,
                     "maxMessagesPerSecond": max_per_second, "maxBytesPerSecond": 262144},
            log_group="/dda/workflow-debug/" + self.usecase_id, max_sessions_per_usecase=4)
        self.sessions_client.transact_write_items(TransactItems=s.serialize_actions(actions))
        if state is not None:
            for sk in ("META", "PAIR#" + self.device):
                self.table.update_item(Key={"pk": "SESSION#" + session_id, "sk": sk},
                                       UpdateExpression="SET #s = :s",
                                       ExpressionAttributeNames={"#s": "state"},
                                       ExpressionAttributeValues={":s": state})
        return session_id

    def stop_session(self, session_id=None):
        from workflow_debug import sessions as s
        session_id = session_id or self.session_id
        meta = self.table.get_item(Key={"pk": "SESSION#" + session_id, "sk": "META"})["Item"]
        pair = self.table.get_item(Key={"pk": "SESSION#" + session_id, "sk": "PAIR#" + self.device})["Item"]
        actions = s.stop_actions(TABLE, meta, [pair], new_state="stopped", now_ms=int(self.clock() * 1000))
        self.sessions_client.transact_write_items(TransactItems=s.serialize_actions(actions))

    def envelope(self, seq=1, records=None, **overrides):
        document = {
            "schema": "dda.debug.v1", "sessionId": self.session_id, "instance": self.instance,
            "deviceId": self.device, "workflowId": self.workflow_id, "workflowVersion": "1",
            "seq": seq, "sentAt": int(self.clock() * 1000),
            "records": records if records is not None else [
                {"type": "run", "ts": 1792075062500, "executionId": "e-1", "status": "completed",
                 "trigger": {"payload": "password=" + SECRET_MARKER}},
                {"type": "log", "ts": 1792075062600, "level": "INFO", "logger": "x", "message": "m"},
            ],
        }
        document.update(overrides)
        return document

    def event(self, document=None, *, topic=None, c=None, pr=PR, raw=None):
        payload = raw if raw is not None else json.dumps(document or self.envelope()).encode("utf-8")
        return {
            "p": base64.b64encode(payload).decode("ascii"),
            "t": topic or "dda/debug/{0}/{1}/record".format(self.usecase_id, self.workflow_id),
            "pr": pr,
            "c": c if c is not None else self.device + "#2",
            "r": int(self.clock() * 1000) + 5,
            "m": str(uuid.uuid4()),
        }

    def context(self, account=PORTAL_ACCOUNT, qualified=True):
        arn = "arn:aws:lambda:{0}:{1}:function:dda-portal-workflow-debug-ingest".format(REGION, PORTAL_ACCOUNT)
        if qualified:
            arn += ":acct-" + account
        return SimpleNamespace(invoked_function_arn=arn)

    def run(self, event=None, context=None):
        return self.ingest.process(event or self.event(), context or self.context(),
                                   clock=self.clock, sleep=lambda s: None)

    def seq_items(self):
        response = self.table.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("pk").eq(
            "SESSION#" + self.session_id) & boto3.dynamodb.conditions.Key("sk").begins_with("SEQ#"))
        return response.get("Items", [])

    def meta(self):
        return self.table.get_item(Key={"pk": "SESSION#" + self.session_id, "sk": "META"})["Item"]


@pytest.fixture
def world(aws_stack, monkeypatch):
    monkeypatch.setenv("WORKFLOW_DEBUG_SESSIONS_TABLE", TABLE)
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL_ACCOUNT)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("DEBUG_LOG_RETENTION_DAYS", "14")
    ensure_sessions_table()
    for name in list(sys.modules):
        if name == "workflow_debug_ingest" or name.startswith("edge_telemetry_common") or name.startswith("workflow_debug"):
            sys.modules.pop(name, None)
    import workflow_debug_ingest as ingest
    from edge_telemetry_common import credentials
    logs = StubLogs()
    iot = FakeIot({})
    monkeypatch.setattr(ingest, "_logs", lambda: logs)
    monkeypatch.setattr(credentials, "client", lambda service, usecase, purpose, region=None, **kw: iot)
    ingest.reset_caches()
    clock = Clock()
    yield World(aws_stack, ingest, logs, iot, clock)
    ingest.reset_caches()


class TestAccept:
    def test_full_accept_writes_the_stamped_events(self, world, capsys):
        world.create_session()
        result = world.run()
        assert result["result"] == "accepted" and result["events"] == 2
        assert result["group"] == "/dda/workflow-debug/" + world.usecase_id
        assert result["stream"] == world.device + "/" + world.workflow_id
        puts = world.logs.puts()
        assert len(puts) == 1
        put = puts[0]
        assert put["logGroupName"] == result["group"] and put["logStreamName"] == result["stream"]
        messages = [json.loads(e["message"]) for e in put["logEvents"]]
        write_ts = int(world.clock() * 1000)
        assert [m["id"] for m in messages] == ["{0}:{1}:1:{2}".format(world.session_id, world.instance, i)
                                               for i in range(2)]
        assert {m["writeTs"] for m in messages} == {write_ts}
        assert {e["timestamp"] for e in put["logEvents"]} == {write_ts}
        assert {m["receivedMs"] for m in messages} == {write_ts + 5}
        assert [m["ts"] for m in messages] == [1792075062500, 1792075062600]
        # The second redaction pass ran.
        assert SECRET_MARKER not in put["logEvents"][0]["message"]
        assert "password=***" in messages[0]["trigger"]["payload"]
        # The dedup mark and the counters.
        items = world.seq_items()
        assert len(items) == 1 and items[0]["sk"] == "SEQ#{0}#1".format(world.instance)
        assert int(items[0]["expires_at"]) == int(world.clock()) + 600
        meta = world.meta()
        assert int(meta["counters"]["records"]) == 2 and int(meta["counters"]["bytes"]) == result["bytes"]
        # The EMF line.
        out = capsys.readouterr().out
        lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        accepted = [l for l in lines if l.get("Accepted") == 1]
        assert accepted and accepted[0]["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "DDA/WorkflowDebug"
        assert accepted[0]["Account"] == PORTAL_ACCOUNT and accepted[0]["UseCaseId"] == world.usecase_id
        assert accepted[0]["BytesWritten"] == result["bytes"]

    def test_pending_sessions_are_accepted(self, world):
        world.create_session(state="pending")
        assert world.run()["result"] == "accepted"

    def test_the_identity_lookup_uses_the_cached_thing(self, world):
        world.create_session()
        world.run()
        world.run(world.event(world.envelope(seq=2)))
        assert len(world.iot.calls) == 1
        assert world.iot.calls[0]["principal"] == "arn:aws:iot:{0}:{1}:cert/{2}".format(REGION, PORTAL_ACCOUNT, PR)


class TestRejections:
    def run_rejected(self, world, event=None, context=None):
        puts_before, marks_before = len(world.logs.puts()), len(world.seq_items())
        result = world.run(event, context)
        assert result["result"] == "rejected", result
        assert len(world.logs.puts()) == puts_before, "a rejected message wrote to the stream"
        assert len(world.seq_items()) == marks_before, "a rejected message left a dedup mark"
        return result["reason"]

    def test_invalid_json(self, world):
        world.create_session()
        event = world.event()
        event["p"] = "not*base64"
        assert self.run_rejected(world, event) == "invalid_json"
        assert self.run_rejected(world, world.event(raw=b"[1,2]")) == "invalid_json"

    def test_too_large(self, world):
        world.create_session()
        document = world.envelope()
        document["records"][0]["padding"] = "x" * (128 * 1024)
        assert self.run_rejected(world, world.event(document)) == "too_large"

    def test_wrong_topic_is_attribution_mismatch(self, world):
        world.create_session()
        telemetry = "dda/telemetry/{0}/{1}/summary".format(world.usecase_id, world.workflow_id)
        assert self.run_rejected(world, world.event(topic=telemetry)) == "attribution_mismatch"
        assert self.run_rejected(world, world.event(topic="ppe-demo/invoke")) == "attribution_mismatch"
        other_uc = "dda/debug/uc-{0}/{1}/record".format(uuid.uuid4(), world.workflow_id)
        assert self.run_rejected(world, world.event(topic=other_uc)) == "attribution_mismatch"

    def test_unknown_kind_is_schema_unknown(self, world):
        world.create_session()
        topic = "dda/debug/{0}/{1}/summary".format(world.usecase_id, world.workflow_id)
        assert self.run_rejected(world, world.event(topic=topic)) == "schema_unknown"

    def test_unqualified_invocation_and_another_account(self, world):
        world.create_session()
        assert self.run_rejected(world, context=world.context(qualified=False)) == "attribution_mismatch"
        assert self.run_rejected(world, context=world.context(account="222222222222")) == "attribution_mismatch"

    def test_client_id_that_does_not_match_the_thing(self, world):
        world.create_session()
        assert self.run_rejected(world, world.event(c="another-thing#1")) == "attribution_mismatch"

    def test_unresolved_principal_is_unattributed(self, world):
        world.create_session()
        assert self.run_rejected(world, world.event(pr="cd" * 32)) == "unattributed"
        assert self.run_rejected(world, world.event(pr="AIDAEXAMPLE:session")) == "unattributed"

    def test_device_without_a_device_record(self, world):
        world.create_session()
        world.stack.tables.devices.delete_item(Key={"device_id": world.device})
        assert self.run_rejected(world) == "attribution_mismatch"

    def test_device_recorded_under_another_use_case(self, world):
        world.create_session()
        world.stack.tables.devices.put_item(Item={"device_id": world.device, "usecase_id": "uc-other"})
        assert self.run_rejected(world) == "attribution_mismatch"

    def test_workflow_not_in_usecase(self, world):
        world.create_session()
        world.stack.tables.workflows.put_item(Item={"workflow_id": world.workflow_id, "usecase_id": "uc-other",
                                                    "created_at": 1})
        assert self.run_rejected(world) == "workflow_not_in_usecase"
        world.stack.tables.workflows.delete_item(Key={"workflow_id": world.workflow_id})
        world.ingest.reset_caches()
        world.iot.things[PR] = world.device
        assert self.run_rejected(world) == "workflow_not_in_usecase"

    def test_envelope_ids_must_match(self, world):
        world.create_session()
        assert self.run_rejected(world, world.event(world.envelope(deviceId="other"))) == "attribution_mismatch"
        assert self.run_rejected(world, world.event(world.envelope(workflowId=str(uuid.uuid4())))) == "attribution_mismatch"

    def test_session_inactive_without_any_session(self, world):
        assert self.run_rejected(world) == "session_inactive"

    def test_session_inactive_for_a_stopped_or_expired_session(self, world):
        world.create_session()
        assert world.run()["result"] == "accepted"
        world.stop_session()
        world.ingest.reset_caches()
        world.logs.calls.clear()
        assert self.run_rejected(world, world.event(world.envelope(seq=2))) == "session_inactive"
        world.ingest.reset_caches()
        world.session_id = str(uuid.uuid4())
        world.create_session(expires_in_s=-60)
        assert self.run_rejected(world) == "session_inactive"

    def test_session_inactive_for_another_session_id(self, world):
        world.create_session()
        assert self.run_rejected(world, world.event(world.envelope(sessionId=str(uuid.uuid4())))) == "session_inactive"

    def test_schema_invalid(self, world):
        world.create_session()
        assert self.run_rejected(world, world.event(world.envelope(seq=0))) == "schema_invalid"
        assert self.run_rejected(world, world.event(world.envelope(instance="nope"))) == "schema_invalid"
        assert self.run_rejected(world, world.event(world.envelope(records=[]))) == "schema_invalid"

    def test_rate_exceeded_at_twice_the_cap_over_ten_seconds(self, world):
        world.create_session(max_per_second=1)
        for seq in range(1, 21):
            assert world.run(world.event(world.envelope(seq=seq)))["result"] == "accepted"
        result = world.run(world.event(world.envelope(seq=21)))
        assert result["result"] == "rejected" and result["reason"] == "rate_exceeded"
        assert len(world.logs.puts()) == 20
        # The window slides: eleven seconds later the Pair may publish again.
        world.clock.now += 11
        assert world.run(world.event(world.envelope(seq=22)))["result"] == "accepted"


class TestDedupProtocol:
    def test_throttled_write_raises_and_leaves_no_mark_then_retry_writes_once(self, world):
        world.create_session()
        world.logs.put_failures = [client_error("ThrottlingException")] * 4
        event = world.event()
        with pytest.raises(world.ingest.logs_writer.PutThrottled):
            world.run(event)
        assert world.seq_items() == []
        assert len(world.logs.puts()) == 4
        # Lambda redelivers the same event: written, not a duplicate.
        second = world.run(event)
        assert second["result"] == "accepted"
        assert len(world.logs.puts()) == 5
        assert len(world.seq_items()) == 1
        # A third delivery after the success is a duplicate and writes nothing.
        third = world.run(event)
        assert third["result"] == "duplicate" and third["reason"] == "duplicate"
        assert len(world.logs.puts()) == 5
        assert int(world.meta()["counters"]["records"]) == 2

    def test_a_new_instance_never_collides_with_the_previous_one(self, world):
        world.create_session()
        assert world.run()["result"] == "accepted"
        world.instance = str(uuid.uuid4())
        assert world.run()["result"] == "accepted"
        assert len(world.seq_items()) == 2


class TestLogGroupRecreation:
    def test_missing_group_is_recreated_inline(self, world):
        world.create_session()
        world.logs.put_failures = [client_error("ResourceNotFoundException")]
        world.logs.describe_raises = client_error("ResourceNotFoundException", "DescribeLogStreams")
        result = world.run()
        assert result["result"] == "accepted"
        names = world.logs.names()
        assert names == ["create_log_stream", "put_log_events", "describe_log_streams", "create_log_group",
                         "put_retention_policy", "tag_resource", "create_log_stream", "put_log_events"]
        retention = [k for n, k in world.logs.calls if n == "put_retention_policy"][0]
        assert retention == {"logGroupName": "/dda/workflow-debug/" + world.usecase_id, "retentionInDays": 14}
        tag = [k for n, k in world.logs.calls if n == "tag_resource"][0]
        assert tag["tags"] == {"dda-portal:usecase-id": world.usecase_id}
        assert tag["resourceArn"].endswith(":log-group:/dda/workflow-debug/" + world.usecase_id)
        assert len(world.seq_items()) == 1

    def test_failed_recreation_counts_log_group_missing_and_raises(self, world, capsys):
        world.create_session()
        world.logs.put_failures = [client_error("ResourceNotFoundException")]
        world.logs.describe_raises = client_error("ResourceNotFoundException", "DescribeLogStreams")
        world.logs.create_log_group = lambda **kwargs: (_ for _ in ()).throw(
            client_error("AccessDeniedException", "CreateLogGroup"))
        with pytest.raises(world.ingest.logs_writer.LogGroupMissing):
            world.run()
        assert world.seq_items() == []
        out = capsys.readouterr().out
        assert any(json.loads(l).get("Reason") == "log_group_missing" for l in out.splitlines() if l.startswith("{"))


class TestIdentityErrors:
    def test_retryable_lookup_raises_for_lambdas_retry(self, world):
        world.create_session()
        world.iot.raise_next = [client_error("ThrottlingException", "ListPrincipalThings")]
        with pytest.raises(world.ingest.identity.LookupRetryable):
            world.run()
        assert world.logs.puts() == []
        assert world.run()["result"] == "accepted"

    def test_denied_lookup_is_terminal_identity_unavailable(self, world, capsys):
        world.create_session()
        world.iot.raise_next = [client_error("AccessDeniedException", "ListPrincipalThings")]
        result = world.run()
        assert result == {"result": "rejected", "reason": "identity_unavailable"}
        # Cached: the next message of the account is refused without a call.
        assert world.run()["reason"] == "identity_unavailable"
        assert len(world.iot.calls) == 1
        out = capsys.readouterr().out
        lines = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
        assert any(l.get("Reason") == "identity_unavailable" for l in lines)
        alarm_lines = [l for l in lines if l.get("IdentityUnavailable") == 1]
        assert len(alarm_lines) == 2, "one dimensionless IdentityUnavailable sample per terminal refusal"
        assert alarm_lines[0]["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [[]]


class TestNoPayloadInLogs:
    def test_handler_logs_never_carry_a_payload_string(self, world, caplog):
        world.create_session()
        caplog.set_level(logging.DEBUG)
        world.run()
        world.run(world.event(world.envelope(seq=1)))  # duplicate
        world.run(world.event(world.envelope(seq=0)))  # schema_invalid
        world.run(world.event(world.envelope(seq=2, sessionId=str(uuid.uuid4()))))  # session_inactive
        event = world.event()
        event["p"] = "not*base64"
        world.run(event)
        world.run(world.event(c="other"))
        text = "\n".join(record.getMessage() for record in caplog.records)
        assert SECRET_MARKER not in text
        ours = [record for record in caplog.records if record.name.startswith("workflow_debug")]
        assert ours, "the handler logged its rejections"
        own_text = "\n".join(record.getMessage() for record in ours)
        assert "executionId" not in own_text and "password" not in own_text
        for record in ours:
            assert len(record.getMessage()) < 512


class TestHandler:
    def test_handler_entry_point(self, world):
        # The entry point uses the wall clock: the session must outlive it.
        world.create_session(expires_in_s=10 * 365 * 24 * 3600)
        result = world.ingest.handler(world.event(), world.context())
        assert result["result"] == "accepted"
        assert world.ingest.handler("not an object", world.context()) == {"result": "rejected", "reason": "invalid_json"}
