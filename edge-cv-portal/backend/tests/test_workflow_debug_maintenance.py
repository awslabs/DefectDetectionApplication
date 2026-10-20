"""``workflow_debug_maintenance`` (workflow-debug-mode design §7.4; tasks.md
3.7) on the moto stack: an expired session is marked, its shadow entries
nulled, its markers deleted, the counters decremented and the audit row
written; a ``pending`` session with a failed write is retried; a missing group
is recreated; nothing happens with no session.
"""
import json
import sys
import uuid

import boto3
import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"
PORTAL_ACCOUNT = "111111111111"
#: One table per test: the maintenance scans the whole table, so a shared
#: table would make the live count depend on the other tests of the module.
TABLE = "test-workflow-debug-maintenance"


def client_error(code, operation, status=400):
    return ClientError({"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
                       operation)


class StubLogs:
    def __init__(self):
        self.calls = []
        self.existing = set()

    def create_log_group(self, **kwargs):
        self.calls.append(("create_log_group", kwargs))
        if kwargs["logGroupName"] in self.existing:
            raise client_error("ResourceAlreadyExistsException", "CreateLogGroup")
        self.existing.add(kwargs["logGroupName"])
        return {}

    def put_retention_policy(self, **kwargs):
        self.calls.append(("put_retention_policy", kwargs))
        return {}

    def tag_resource(self, **kwargs):
        self.calls.append(("tag_resource", kwargs))
        return {}

    def names(self):
        return [n for n, _ in self.calls]


class StubIotData:
    def __init__(self):
        self.updates = []
        self.fail = {}

    def update_thing_shadow(self, **kwargs):
        self.updates.append(kwargs)
        failure = self.fail.get(kwargs["thingName"])
        if failure:
            raise failure
        return {}


def ensure_sessions_table():
    client = boto3.client("dynamodb", region_name=REGION)
    if TABLE in client.list_tables()["TableNames"]:
        client.delete_table(TableName=TABLE)
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


class World:
    def __init__(self, env, maintenance, logs, iotdata):
        self.env = env
        self.maintenance = maintenance
        self.logs = logs
        self.iotdata = iotdata
        self.usecase_id = env.create_usecase()
        env.stack.tables.usecases.update_item(
            Key={"usecase_id": self.usecase_id},
            UpdateExpression="SET account_id = :a, #r = :r",
            ExpressionAttributeNames={"#r": "region"},
            ExpressionAttributeValues={":a": PORTAL_ACCOUNT, ":r": REGION})
        self.workflow_id = str(uuid.uuid4())
        self.client = boto3.client("dynamodb", region_name=REGION)
        self.table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)

    def create(self, devices, *, enabled_at, expires_at, shadow_results=None, activate=False):
        from workflow_debug import sessions as s
        session_id = str(uuid.uuid4())
        actions = s.creation_actions(
            TABLE, session_id=session_id, usecase_id=self.usecase_id, workflow_id=self.workflow_id,
            workflow_version="1", devices=devices, all_devices=len(devices) > 1, enabled_at=enabled_at,
            expires_at=expires_at, actor="user-1",
            options={"level": "debug", "images": False, "imageMaxKb": 150, "maxMessagesPerSecond": 10,
                     "maxBytesPerSecond": 262144, "durationMinutes": 60},
            log_group="/dda/workflow-debug/" + self.usecase_id, max_sessions_per_usecase=4,
            media={"bucket": "dda-inference-results-" + PORTAL_ACCOUNT, "region": "eu-west-1", "region_source": "header"})
        self.client.transact_write_items(TransactItems=s.serialize_actions(actions))
        if shadow_results:
            self.client.transact_write_items(TransactItems=s.serialize_actions(
                s.shadow_result_actions(TABLE, session_id, shadow_results, activate=activate)))
        return session_id

    def meta(self, session_id):
        return self.table.get_item(Key={"pk": "SESSION#" + session_id, "sk": "META"}).get("Item")

    def pair(self, session_id, device):
        return self.table.get_item(Key={"pk": "SESSION#" + session_id, "sk": "PAIR#" + device}).get("Item")

    def lock(self, device):
        from workflow_debug import sessions as s
        return self.table.get_item(Key=s.pairlock_key(self.usecase_id, device, self.workflow_id)).get("Item")

    def counter(self, pk):
        item = self.table.get_item(Key={"pk": pk, "sk": "ACTIVE"}).get("Item") or {}
        return int(item.get("count", 0))

    def audits(self, action):
        return [r for r in self.env.stack.tables.audit_log.scan()["Items"] if r.get("action") == action]


@pytest.fixture
def world(env, monkeypatch):
    monkeypatch.setenv("WORKFLOW_DEBUG_SESSIONS_TABLE", TABLE)
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL_ACCOUNT)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("DEBUG_LOG_RETENTION_DAYS", "14")
    ensure_sessions_table()
    for name in list(sys.modules):
        if name == "workflow_debug_maintenance" or name.startswith("edge_telemetry_common") or name.startswith("workflow_debug"):
            sys.modules.pop(name, None)
    import workflow_debug_maintenance as maintenance
    from edge_telemetry_common import credentials
    logs, iotdata = StubLogs(), StubIotData()
    monkeypatch.setattr(maintenance, "_logs", lambda: logs)
    monkeypatch.setattr(credentials, "client", lambda service, usecase, purpose, region=None, **kw: iotdata)
    maintenance.reset_clients()
    yield World(env, maintenance, logs, iotdata)
    maintenance.reset_clients()


NOW = 1792075062000


class TestExpiry:
    def test_expired_session_is_marked_nulled_unlocked_decremented_and_audited(self, world):
        device_a, device_b = "thing-a-" + world.usecase_id[-6:], "thing-b-" + world.usecase_id[-6:]
        session_id = world.create([device_a, device_b], enabled_at=NOW - 3_600_000, expires_at=NOW - 1,
                                  shadow_results={device_a: {"result": "ok"}, device_b: {"result": "ok"}}, activate=True)
        world.logs.existing.add("/dda/workflow-debug/" + world.usecase_id)
        assert world.meta(session_id)["state"] == "active"
        assert world.counter("UC#" + world.usecase_id) == 1 and world.counter("DEV#" + device_a) == 1
        summary = world.maintenance.run(now_ms=NOW)
        assert summary["expired"] == [session_id] and summary["live"] == 1 and summary["errors"] == 0
        assert world.meta(session_id)["state"] == "expired" and int(world.meta(session_id)["stoppedAt"]) == NOW
        assert world.pair(session_id, device_a)["state"] == "expired"
        assert world.pair(session_id, device_b)["state"] == "expired"
        assert world.lock(device_a) is None and world.lock(device_b) is None
        assert world.counter("UC#" + world.usecase_id) == 0
        assert world.counter("DEV#" + device_a) == 0 and world.counter("DEV#" + device_b) == 0
        # The shadow entries were nulled.
        assert sorted(u["thingName"] for u in world.iotdata.updates) == sorted([device_a, device_b])
        for update in world.iotdata.updates:
            assert json.loads(update["payload"]) == {"state": {"desired": {"v": 1, "sessions": {world.workflow_id: None}}}}
            assert update["shadowName"] == "dda-workflow-debug"
        rows = [r for r in world.audits("debug_session_expired") if r["resource_id"] == session_id]
        assert len(rows) == 1 and rows[0]["user_id"] == "system"
        assert sorted(rows[0]["details"]["devices"]) == sorted([device_a, device_b])
        # A second run finds nothing live.
        world.iotdata.updates.clear()
        summary = world.maintenance.run(now_ms=NOW)
        assert summary["live"] == 0 and world.iotdata.updates == []

    def test_a_failed_null_write_still_expires_the_session(self, world):
        device = "thing-c-" + world.usecase_id[-6:]
        world.iotdata.fail[device] = client_error("ResourceNotFoundException", "UpdateThingShadow", 404)
        session_id = world.create([device], enabled_at=NOW - 3_600_000, expires_at=NOW - 1)
        summary = world.maintenance.run(now_ms=NOW)
        assert summary["expired"] == [session_id]
        assert world.meta(session_id)["state"] == "expired" and world.lock(device) is None
        rows = [r for r in world.audits("debug_session_expired") if r["resource_id"] == session_id]
        assert rows[0]["details"]["shadow"] == {device: "failed"}

    def test_a_session_expiring_later_is_left_alone(self, world):
        device = "thing-d-" + world.usecase_id[-6:]
        session_id = world.create([device], enabled_at=NOW, expires_at=NOW + 60_000,
                                  shadow_results={device: {"result": "ok"}}, activate=True)
        world.logs.existing.add("/dda/workflow-debug/" + world.usecase_id)
        summary = world.maintenance.run(now_ms=NOW)
        assert summary["expired"] == [] and summary["live"] == 1
        assert world.meta(session_id)["state"] == "active" and world.lock(device) is not None
        assert world.iotdata.updates == []


class TestPendingRetry:
    def test_failed_pending_write_is_retried_and_the_session_activates(self, world):
        device_a, device_b = "thing-e-" + world.usecase_id[-6:], "thing-f-" + world.usecase_id[-6:]
        session_id = world.create([device_a, device_b], enabled_at=NOW, expires_at=NOW + 3_600_000,
                                  shadow_results={device_a: {"result": "ok"},
                                                  device_b: {"result": "failed", "error": "throttled"}}, activate=False)
        world.logs.existing.add("/dda/workflow-debug/" + world.usecase_id)
        assert world.meta(session_id)["state"] == "pending"
        summary = world.maintenance.run(now_ms=NOW)
        assert summary["retried"] == 1 and summary["activated"] == [session_id]
        # Only the failed device was rewritten, with the entry the API would write.
        assert [u["thingName"] for u in world.iotdata.updates] == [device_b]
        entry = json.loads(world.iotdata.updates[0]["payload"])["state"]["desired"]["sessions"][world.workflow_id]
        assert entry == {
            "sessionId": session_id, "usecaseId": world.usecase_id, "enabledAt": NOW, "expiresAt": NOW + 3_600_000,
            "level": "debug", "images": False, "imageMaxKb": 150,
            "bucket": "dda-inference-results-" + PORTAL_ACCOUNT, "region": "eu-west-1",
            "prefix": "dda-telemetry/debug/{0}/{1}/{2}/{3}/".format(world.usecase_id, device_b, world.workflow_id, session_id),
            "maxMessagesPerSecond": 10, "maxBytesPerSecond": 262144,
        }
        assert world.meta(session_id)["state"] == "active"
        assert world.pair(session_id, device_b)["state"] == "active"
        assert world.pair(session_id, device_b)["shadow"]["result"] == "ok"
        assert world.pair(session_id, device_a)["state"] == "active"

    def test_a_write_that_keeps_failing_leaves_the_session_pending(self, world):
        device = "thing-g-" + world.usecase_id[-6:]
        world.iotdata.fail[device] = client_error("AccessDeniedException", "UpdateThingShadow", 403)
        session_id = world.create([device], enabled_at=NOW, expires_at=NOW + 3_600_000,
                                  shadow_results={device: {"result": "failed", "error": "thing_not_found"}}, activate=False)
        world.logs.existing.add("/dda/workflow-debug/" + world.usecase_id)
        summary = world.maintenance.run(now_ms=NOW)
        assert summary["retried"] == 1 and summary["activated"] == []
        assert world.meta(session_id)["state"] == "pending"
        assert world.pair(session_id, device)["shadow"] == {"result": "failed", "error": "access_denied",
                                                            "writtenAt": world.pair(session_id, device)["shadow"]["writtenAt"]}


class TestLogGroup:
    def test_a_missing_group_is_recreated_with_retention_and_tag(self, world):
        device = "thing-h-" + world.usecase_id[-6:]
        world.create([device], enabled_at=NOW, expires_at=NOW + 3_600_000,
                     shadow_results={device: {"result": "ok"}}, activate=True)
        summary = world.maintenance.run(now_ms=NOW)
        assert summary["recreated_groups"] == 1
        group = "/dda/workflow-debug/" + world.usecase_id
        assert world.logs.names() == ["create_log_group", "put_retention_policy", "tag_resource"]
        assert world.logs.calls[1][1] == {"logGroupName": group, "retentionInDays": 14}
        assert world.logs.calls[2][1] == {"resourceArn": "arn:aws:logs:us-east-1:111111111111:log-group:" + group,
                                          "tags": {"dda-portal:usecase-id": world.usecase_id}}
        # A present group costs one CreateLogGroup that is refused, nothing more.
        world.logs.calls.clear()
        summary = world.maintenance.run(now_ms=NOW)
        assert summary["recreated_groups"] == 0 and world.logs.names() == ["create_log_group"]

    def test_a_denied_recreation_is_logged_not_raised(self, world):
        device = "thing-i-" + world.usecase_id[-6:]
        world.create([device], enabled_at=NOW, expires_at=NOW + 3_600_000,
                     shadow_results={device: {"result": "ok"}}, activate=True)
        world.logs.create_log_group = lambda **kwargs: (_ for _ in ()).throw(
            client_error("AccessDeniedException", "CreateLogGroup", 403))
        summary = world.maintenance.run(now_ms=NOW)
        assert summary["recreated_groups"] == 0 and summary["errors"] == 0


class TestNothingToDo:
    def test_no_session_means_no_call(self, world):
        summary = world.maintenance.run(now_ms=NOW)
        assert summary["live"] == 0 and summary["expired"] == [] and summary["retried"] == 0
        assert world.iotdata.updates == [] and world.logs.calls == []

    def test_handler_entry_point(self, world):
        assert world.maintenance.handler({}, None)["live"] == 0
