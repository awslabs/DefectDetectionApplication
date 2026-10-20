"""``workflow_debug.sessions`` (workflow-debug-mode design §7.1; tasks.md 3.4):
the creation action list and its ``3N + 2`` bound with a conditional ``Put``
for ``PAIRLOCK`` (never a ``ConditionCheck``), the cancellation mapping, the
transitions, the stop transaction, and the transaction against moto: two
creations for one Pair and exactly one commits.
"""
import uuid

import boto3
import pytest
from botocore.exceptions import ClientError

from workflow_debug import sessions as s

UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
TABLE = "test-workflow-debug-sessions"
OPTIONS = {"level": "debug", "images": False, "imageMaxKb": 150, "maxMessagesPerSecond": 10,
           "maxBytesPerSecond": 262144}


def creation(devices, session_id=None, **overrides):
    kwargs = dict(session_id=session_id or str(uuid.uuid4()), usecase_id=UC, workflow_id=WF,
                  workflow_version="1", devices=devices, all_devices=len(devices) > 1,
                  enabled_at=1792075062000, expires_at=1792078662000, actor="user-1", options=OPTIONS,
                  log_group="/dda/workflow-debug/" + UC, max_sessions_per_usecase=4)
    kwargs.update(overrides)
    return s.creation_actions(TABLE, **kwargs)


class TestKeysAndStates:
    def test_keys(self):
        assert s.session_pk("sid") == "SESSION#sid"
        assert s.pair_sk("thing") == "PAIR#thing" and s.device_of_pair_sk("PAIR#thing") == "thing"
        assert s.seq_sk("inst", 7) == "SEQ#inst#7"
        assert s.usecase_counter_key(UC) == {"pk": "UC#" + UC, "sk": "ACTIVE"}
        assert s.device_counter_key("thing") == {"pk": "DEV#thing", "sk": "ACTIVE"}
        assert s.pairlock_key(UC, "thing", WF) == {"pk": "PAIRLOCK#{0}#thing#{1}".format(UC, WF), "sk": "LOCK"}
        assert s.by_pair_pk(UC, "thing", WF) == "P#{0}#thing#{1}".format(UC, WF)
        assert s.gsi_sort(1792078662000, "sid") == "1792078662000#sid"
        assert s.gsi_sort(5, "sid") == "0000000000005#sid"

    def test_transitions(self):
        assert s.can_transition("pending", "active")
        assert s.can_transition("pending", "stopped") and s.can_transition("pending", "expired")
        assert s.can_transition("active", "stopped") and s.can_transition("active", "expired")
        assert not s.can_transition("active", "pending")
        assert not s.can_transition("stopped", "active") and not s.can_transition("expired", "stopped")
        assert not s.can_transition("nope", "active")
        assert s.is_live("pending") and s.is_live("active")
        assert not s.is_live("stopped") and not s.is_live("expired") and not s.is_live(None)

    def test_seq_item_has_a_ten_minute_ttl(self):
        item = s.seq_item("sid", "inst", 3, 1792075062000)
        assert item == {"pk": "SESSION#sid", "sk": "SEQ#inst#3", "writtenAt": 1792075062000,
                        "expires_at": 1792075062 + 600}


class TestCreationActions:
    @pytest.mark.parametrize("n", [1, 2, 20, 32])
    def test_three_n_plus_two_actions_within_the_limit(self, n):
        devices = ["thing-%02d" % i for i in range(n)]
        actions = creation(devices)
        assert len(actions) == 3 * n + 2 <= 100
        kinds = [next(iter(a)) for a in actions]
        assert kinds == ["Put"] + ["Put"] * n + ["Update"] + ["Update"] * n + ["Put"] * n
        assert "ConditionCheck" not in kinds

    def test_thirty_three_devices_are_refused(self):
        with pytest.raises(ValueError):
            creation(["thing-%02d" % i for i in range(33)])
        with pytest.raises(ValueError):
            creation([])
        with pytest.raises(ValueError):
            creation(["a", "a"])

    def test_meta_item(self):
        actions = creation(["thing-a", "thing-b"], session_id="sid-1")
        meta = actions[0]["Put"]
        assert meta["TableName"] == TABLE
        assert meta["ConditionExpression"] == "attribute_not_exists(pk)"
        item = meta["Item"]
        assert item["pk"] == "SESSION#sid-1" and item["sk"] == "META"
        assert item["state"] == "pending" and item["device_ids"] == ["thing-a", "thing-b"]
        assert item["all_devices"] is True and item["usecase_id"] == UC and item["workflow_id"] == WF
        assert item["enabledAt"] == 1792075062000 and item["expiresAt"] == 1792078662000
        assert item["options"] == OPTIONS and item["counters"] == {"records": 0, "bytes": 0}
        assert item["logGroup"] == "/dda/workflow-debug/" + UC and item["actor"] == "user-1"
        assert item["gsi_usecase"] == "UC#" + UC and item["gsi_sort"] == "1792078662000#sid-1"
        assert item["expires_at"] == 1792078662 + 30 * 24 * 3600

    def test_pair_items_carry_the_precomputed_stream_name(self):
        actions = creation(["site:line-3:cam_01"], session_id="sid-1")
        pair = actions[1]["Put"]
        assert "ConditionExpression" not in pair
        item = pair["Item"]
        assert item["pk"] == "SESSION#sid-1" and item["sk"] == "PAIR#site:line-3:cam_01"
        assert item["logStream"] == "site_line-3_cam_01/" + WF
        assert item["state"] == "pending" and item["sessionId"] == "sid-1" and item["device_id"] == "site:line-3:cam_01"
        assert item["shadow"] == {"result": "pending"} and item["reported"] is None
        assert item["gsi_pair"] == "P#{0}#site:line-3:cam_01#{1}".format(UC, WF)
        assert item["gsi_sort"] == "1792078662000#sid-1" and item["expiresAt"] == 1792078662000

    def test_counter_updates(self):
        actions = creation(["thing-a", "thing-b"])
        usecase = actions[3]["Update"]
        assert usecase["Key"] == {"pk": "UC#" + UC, "sk": "ACTIVE"}
        assert usecase["UpdateExpression"] == "ADD #count :one"
        assert usecase["ConditionExpression"] == "attribute_not_exists(#count) OR #count < :max"
        assert usecase["ExpressionAttributeValues"] == {":one": 1, ":max": 4}
        for index, device in ((4, "thing-a"), (5, "thing-b")):
            dev = actions[index]["Update"]
            assert dev["Key"] == {"pk": "DEV#" + device, "sk": "ACTIVE"}
            assert dev["ConditionExpression"] == "attribute_not_exists(#count) OR #count < :cap"
            assert dev["ExpressionAttributeValues"] == {":one": 1, ":cap": 2}

    def test_pairlock_markers_are_conditional_puts(self):
        actions = creation(["thing-a", "thing-b"], session_id="sid-1")
        for index, device in ((6, "thing-a"), (7, "thing-b")):
            lock = actions[index]["Put"]
            assert lock["ConditionExpression"] == "attribute_not_exists(pk)"
            assert lock["Item"]["pk"] == "PAIRLOCK#{0}#{1}#{2}".format(UC, device, WF)
            assert lock["Item"]["sk"] == "LOCK" and lock["Item"]["session_id"] == "sid-1"
            assert lock["ReturnValuesOnConditionCheckFailure"] == "ALL_OLD"

    def test_serialization_round_trip(self):
        actions = creation(["thing-a"], session_id="sid-1")
        typed = s.serialize_actions(actions)
        assert typed[0]["Put"]["Item"]["pk"] == {"S": "SESSION#sid-1"}
        assert typed[0]["Put"]["Item"]["all_devices"] == {"BOOL": False}
        assert typed[0]["Put"]["Item"]["counters"] == {"M": {"records": {"N": "0"}, "bytes": {"N": "0"}}}
        assert typed[2]["Update"]["ExpressionAttributeValues"][":max"] == {"N": "4"}
        assert s.deserialize_item(typed[0]["Put"]["Item"])["device_ids"] == ["thing-a"]
        assert typed[1]["Put"]["Item"]["reported"] == {"NULL": True}


class TestCancellationMapping:
    def reasons(self, n, failing_index, item=None):
        total = 3 * n + 2
        result = [{"Code": "None"}] * total
        result[failing_index] = {"Code": "ConditionalCheckFailed", "Message": "The conditional request failed"}
        if item is not None:
            result[failing_index] = dict(result[failing_index], Item=item)
        return result

    def test_usecase_cap(self):
        devices = ["thing-a", "thing-b"]
        actions = creation(devices)
        code, detail = s.cancellation_to_error(self.reasons(2, 3, {"count": {"N": "4"}}), actions, devices)
        assert code == "debug_session_limit" and detail == {"limit": "usecase", "active": 4}

    def test_device_cap(self):
        devices = ["thing-a", "thing-b"]
        actions = creation(devices)
        code, detail = s.cancellation_to_error(self.reasons(2, 5, {"count": {"N": "2"}}), actions, devices)
        assert code == "debug_session_limit"
        assert detail == {"limit": "device", "active": 2, "device": "thing-b"}

    def test_pairlock_exists(self):
        devices = ["thing-a", "thing-b"]
        actions = creation(devices)
        code, detail = s.cancellation_to_error(
            self.reasons(2, 6, {"session_id": {"S": "other-session"}}), actions, devices)
        assert code == "debug_session_exists" and detail == {"session_id": "other-session", "device": "thing-a"}

    def test_meta_exists(self):
        devices = ["thing-a"]
        actions = creation(devices)
        code, detail = s.cancellation_to_error(self.reasons(1, 0), actions, devices)
        assert code == "debug_session_exists"

    def test_no_failed_reason_falls_back_to_the_usecase_limit(self):
        devices = ["thing-a"]
        actions = creation(devices)
        code, detail = s.cancellation_to_error([{"Code": "None"}] * 5, actions, devices)
        assert code == "debug_session_limit" and detail["active"] is None

    def test_other_cancellation_codes_are_skipped(self):
        devices = ["thing-a"]
        actions = creation(devices)
        reasons = [{"Code": "TransactionConflict"}] + [{"Code": "None"}] * 3 + \
                  [{"Code": "ConditionalCheckFailed", "Item": {"session_id": {"S": "x"}}}]
        assert s.cancellation_to_error(reasons, actions, devices) == (
            "debug_session_exists", {"session_id": "x", "device": "thing-a"})


class TestStopActions:
    def test_stop_transaction(self):
        meta = {"session_id": "sid-1", "usecase_id": UC, "workflow_id": WF}
        pairs = [{"sk": "PAIR#thing-a", "device_id": "thing-a"}, {"sk": "PAIR#thing-b"}]
        actions = s.stop_actions(TABLE, meta, pairs, new_state="stopped", now_ms=1792076000000)
        kinds = [next(iter(a)) for a in actions]
        assert kinds == ["Update", "Update", "Update", "Delete", "Delete", "Update", "Update", "Update"]
        meta_update = actions[0]["Update"]
        assert meta_update["Key"] == {"pk": "SESSION#sid-1", "sk": "META"}
        assert meta_update["ConditionExpression"] == "#state IN (:pending, :active)"
        assert meta_update["ExpressionAttributeValues"][":state"] == "stopped"
        assert meta_update["ExpressionAttributeValues"][":now"] == 1792076000000
        assert actions[1]["Update"]["Key"] == {"pk": "SESSION#sid-1", "sk": "PAIR#thing-a"}
        assert actions[2]["Update"]["Key"] == {"pk": "SESSION#sid-1", "sk": "PAIR#thing-b"}
        assert actions[3]["Delete"]["Key"] == s.pairlock_key(UC, "thing-a", WF)
        assert actions[4]["Delete"]["Key"] == s.pairlock_key(UC, "thing-b", WF)
        assert actions[5]["Update"]["Key"] == s.usecase_counter_key(UC)
        assert actions[5]["Update"]["ExpressionAttributeValues"] == {":minus": -1}
        assert actions[6]["Update"]["Key"] == s.device_counter_key("thing-a")
        assert actions[7]["Update"]["Key"] == s.device_counter_key("thing-b")

    def test_expired_state_and_invalid_state(self):
        meta = {"session_id": "sid-1", "usecase_id": UC, "workflow_id": WF}
        actions = s.stop_actions(TABLE, meta, [], new_state="expired", now_ms=1)
        assert actions[0]["Update"]["ExpressionAttributeValues"][":state"] == "expired"
        with pytest.raises(ValueError):
            s.stop_actions(TABLE, meta, [], new_state="active", now_ms=1)


@pytest.fixture
def sessions_table(aws_stack):
    client = boto3.client("dynamodb", region_name="us-east-1")
    existing = client.list_tables()["TableNames"]
    if TABLE not in existing:
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
    return client


class TestAgainstMoto:
    def test_creation_commits_and_a_second_creation_for_the_pair_is_refused(self, sessions_table):
        usecase_id = "uc-{0}".format(uuid.uuid4())
        device = "thing-{0}".format(uuid.uuid4().hex[:8])
        first = s.creation_actions(TABLE, session_id=str(uuid.uuid4()), usecase_id=usecase_id, workflow_id=WF,
                                   workflow_version="1", devices=[device], all_devices=False,
                                   enabled_at=1, expires_at=2, actor="u", options=OPTIONS, log_group="/g",
                                   max_sessions_per_usecase=4)
        sessions_table.transact_write_items(TransactItems=s.serialize_actions(first))
        second = s.creation_actions(TABLE, session_id=str(uuid.uuid4()), usecase_id=usecase_id, workflow_id=WF,
                                    workflow_version="1", devices=[device], all_devices=False,
                                    enabled_at=1, expires_at=2, actor="u", options=OPTIONS, log_group="/g",
                                    max_sessions_per_usecase=4)
        with pytest.raises(ClientError) as excinfo:
            sessions_table.transact_write_items(TransactItems=s.serialize_actions(second))
        assert excinfo.value.response["Error"]["Code"] == "TransactionCanceledException"
        reasons = excinfo.value.response.get("CancellationReasons") or []
        if reasons:
            codes = [r.get("Code") for r in reasons]
            assert "ConditionalCheckFailed" in codes
            # The PAIRLOCK put (the last action) is what refused it, not a counter.
            assert codes[-1] == "ConditionalCheckFailed"
        # Exactly one session was created: one META and one lock.
        lock = sessions_table.get_item(TableName=TABLE, Key={"pk": {"S": s.pairlock_key(usecase_id, device, WF)["pk"]},
                                                              "sk": {"S": "LOCK"}})["Item"]
        assert lock["session_id"]["S"] == first[0]["Put"]["Item"]["session_id"]
        counter = sessions_table.get_item(TableName=TABLE, Key={"pk": {"S": "UC#" + usecase_id},
                                                                 "sk": {"S": "ACTIVE"}})["Item"]
        assert counter["count"]["N"] == "1"

    def test_the_usecase_cap_refuses_the_fifth_session(self, sessions_table):
        usecase_id = "uc-{0}".format(uuid.uuid4())
        for i in range(4):
            actions = s.creation_actions(TABLE, session_id=str(uuid.uuid4()), usecase_id=usecase_id, workflow_id=WF,
                                         workflow_version="1", devices=["thing-cap-%d-%s" % (i, usecase_id[-4:])],
                                         all_devices=False, enabled_at=1, expires_at=2, actor="u",
                                         options=OPTIONS, log_group="/g", max_sessions_per_usecase=4)
            sessions_table.transact_write_items(TransactItems=s.serialize_actions(actions))
        fifth = s.creation_actions(TABLE, session_id=str(uuid.uuid4()), usecase_id=usecase_id, workflow_id=WF,
                                   workflow_version="1", devices=["thing-cap-5-" + usecase_id[-4:]],
                                   all_devices=False, enabled_at=1, expires_at=2, actor="u",
                                   options=OPTIONS, log_group="/g", max_sessions_per_usecase=4)
        with pytest.raises(ClientError) as excinfo:
            sessions_table.transact_write_items(TransactItems=s.serialize_actions(fifth))
        assert excinfo.value.response["Error"]["Code"] == "TransactionCanceledException"
        # Nothing of the fifth was created.
        assert "Item" not in sessions_table.get_item(
            TableName=TABLE, Key={"pk": {"S": fifth[0]["Put"]["Item"]["pk"]}, "sk": {"S": "META"}})
        assert "Item" not in sessions_table.get_item(
            TableName=TABLE, Key={"pk": {"S": fifth[-1]["Put"]["Item"]["pk"]}, "sk": {"S": "LOCK"}})

    def test_stop_releases_the_pair_and_decrements(self, sessions_table):
        usecase_id = "uc-{0}".format(uuid.uuid4())
        device = "thing-{0}".format(uuid.uuid4().hex[:8])
        session_id = str(uuid.uuid4())
        actions = s.creation_actions(TABLE, session_id=session_id, usecase_id=usecase_id, workflow_id=WF,
                                     workflow_version="1", devices=[device], all_devices=False,
                                     enabled_at=1, expires_at=2, actor="u", options=OPTIONS, log_group="/g",
                                     max_sessions_per_usecase=4)
        sessions_table.transact_write_items(TransactItems=s.serialize_actions(actions))
        meta = actions[0]["Put"]["Item"]
        stop = s.stop_actions(TABLE, meta, [actions[1]["Put"]["Item"]], new_state="stopped", now_ms=5)
        sessions_table.transact_write_items(TransactItems=s.serialize_actions(stop))
        stored = s.deserialize_item(sessions_table.get_item(
            TableName=TABLE, Key={"pk": {"S": "SESSION#" + session_id}, "sk": {"S": "META"}})["Item"])
        assert stored["state"] == "stopped" and stored["stoppedAt"] == 5
        pair = s.deserialize_item(sessions_table.get_item(
            TableName=TABLE, Key={"pk": {"S": "SESSION#" + session_id}, "sk": {"S": "PAIR#" + device}})["Item"])
        assert pair["state"] == "stopped"
        assert "Item" not in sessions_table.get_item(
            TableName=TABLE, Key={"pk": {"S": s.pairlock_key(usecase_id, device, WF)["pk"]}, "sk": {"S": "LOCK"}})
        counter = s.deserialize_item(sessions_table.get_item(
            TableName=TABLE, Key={"pk": {"S": "UC#" + usecase_id}, "sk": {"S": "ACTIVE"}})["Item"])
        assert counter["count"] == 0
        # A second stop fails on the META state condition: the counters move once.
        with pytest.raises(ClientError):
            sessions_table.transact_write_items(TransactItems=s.serialize_actions(stop))
        # The Pair is free again.
        again = s.creation_actions(TABLE, session_id=str(uuid.uuid4()), usecase_id=usecase_id, workflow_id=WF,
                                   workflow_version="1", devices=[device], all_devices=False,
                                   enabled_at=1, expires_at=2, actor="u", options=OPTIONS, log_group="/g",
                                   max_sessions_per_usecase=4)
        sessions_table.transact_write_items(TransactItems=s.serialize_actions(again))
