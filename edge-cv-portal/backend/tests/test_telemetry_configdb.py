"""``telemetry.configdb`` (workflow-telemetry-dashboards design §7.4; tasks.md
3.4): the item keys, NFKC case folding, the expiry filter, the rejection
counter update and the 1 KB sample."""
import json

import pytest

from telemetry import configdb

UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
T0 = 1_792_000_000_000
M0 = (T0 // 60_000) * 60_000      # the minute containing T0 (T0 itself is not minute-aligned)


class TestKeys:
    def test_partition_and_sort_keys(self):
        assert configdb.usecase_pk(UC) == "UC#" + UC
        assert configdb.account_pk("222222222222") == "X#222222222222"
        assert configdb.dash_pk("d1") == "DASH#d1"
        assert configdb.settings_key(UC) == {"pk": "UC#" + UC, "sk": "SETTINGS"}
        assert configdb.SETTINGS_SK == "SETTINGS" and configdb.DASHCOUNT_SK == "DASHCOUNT" and configdb.META_SK == "META"
        assert configdb.RESYNC_SK == "RESYNC" and configdb.SWEEP_SK == "SWEEP" and configdb.MAINT_PK == "MAINT"
        assert configdb.SEEN_SK == "SEEN" and configdb.GG_STATUS_SK == "STATUS"
        assert configdb.lease_pk(UC) == "LEASE#" + UC
        assert configdb.slot_sk(0) == "SLOT#0" and configdb.slot_sk(19) == "SLOT#19"
        assert configdb.holder_sk("d1", "sub-1") == "HOLDER#d1#sub-1"
        assert configdb.devshadow_sk("thing") == "DEVSHADOW#thing"
        assert configdb.gg_pk(UC, "thing") == "GG#{0}#thing".format(UC)
        assert configdb.by_usecase_sk(T0, "d1") == "DASH#{0:013d}#d1".format(T0)
        assert configdb.rej_sk(M0 + 59_999) == "REJ#{0:013d}".format(M0)
        assert configdb.sample_sk(M0 + 1, "schema_invalid") == "SAMPLE#{0:013d}#schema_invalid".format(M0)

    def test_minute_of(self):
        assert configdb.minute_of(T0) % 60_000 == 0 and configdb.minute_of(T0) == M0
        assert configdb.minute_of(M0 + 59_999) == M0
        assert configdb.minute_of(M0 + 60_000) == M0 + 60_000


class TestNames:
    def test_nfkc_case_folding(self):
        assert configdb.fold_name("Line A") == "line a"
        assert configdb.fold_name("  LINE a ") == "line a"
        assert configdb.fold_name("ﬁxture") == "fixture"          # the fi ligature folds to two letters
        assert configdb.fold_name("Straße") == "strasse"
        assert configdb.fold_name("Ｌine") == "line"              # full-width L
        assert configdb.dashname_sk("Line A") == configdb.dashname_sk("line a") == "DASHNAME#line a"
        assert configdb.dashname_sk("Line A") != configdb.dashname_sk("Line B")


class TestExpiry:
    def test_is_unexpired(self):
        assert configdb.is_unexpired({"expires_at": 100}, now_s=99) is True
        assert configdb.is_unexpired({"expires_at": 100}, now_s=100) is False
        assert configdb.is_unexpired({"expires_at": "x"}, now_s=1) is False
        assert configdb.is_unexpired({"pk": "x"}, now_s=1) is True
        assert configdb.is_unexpired(None) is False and configdb.is_unexpired({}) is False

    def test_unexpired_filters(self):
        items = [{"id": 1, "expires_at": 10}, {"id": 2, "expires_at": 1}, {"id": 3}]
        assert [i["id"] for i in configdb.unexpired(items, now_s=5)] == [1, 3]


class TestIngestItems:
    def test_seen_item(self):
        item = configdb.seen_item(UC, T0)
        assert item == {"pk": "UC#" + UC, "sk": "SEEN", "receivedMs": T0, "expires_at": T0 // 1000 + 24 * 3600}

    def test_rejection_update(self):
        update = configdb.rejection_update("UC#" + UC, T0 + 5, {"schema_invalid": 2, "duplicate": 1})
        assert update["Key"] == {"pk": "UC#" + UC, "sk": configdb.rej_sk(T0)}
        assert update["UpdateExpression"] == "ADD #r0 :v0, #r1 :v1 SET #ttl = :ttl, #min = :minute"
        assert update["ExpressionAttributeNames"] == {"#r0": "duplicate", "#r1": "schema_invalid", "#ttl": "expires_at",
                                                      "#min": "minuteMs"}
        assert update["ExpressionAttributeValues"][":v0"] == 1 and update["ExpressionAttributeValues"][":v1"] == 2
        assert update["ExpressionAttributeValues"][":ttl"] == (T0 + 5) // 1000 + 24 * 3600
        assert update["ExpressionAttributeValues"][":minute"] == configdb.minute_of(T0)

    def test_sample_item_is_at_most_one_kilobyte(self):
        sample = {"reason": "schema_invalid", "detail": "x" * 3000, "topic": "dda/telemetry/a/b/summary"}
        item = configdb.sample_item("UC#" + UC, T0, "schema_invalid", sample)
        assert item["sk"] == configdb.sample_sk(T0, "schema_invalid") and item["reason"] == "schema_invalid"
        assert len(json.dumps(item["sample"], separators=(",", ":"))) <= 1024
        assert item["sample"]["topic"] == "dda/telemetry/a/b/summary"
        assert item["expires_at"] == T0 // 1000 + 14 * 24 * 3600
        small = configdb.sample_item("UC#" + UC, T0, "r", {"a": 1})
        assert small["sample"] == {"a": 1}

    def test_query_all_pages(self):
        class Table:
            def __init__(self):
                self.calls = []

            def query(self, **kwargs):
                self.calls.append(kwargs)
                if "ExclusiveStartKey" not in kwargs:
                    return {"Items": [1, 2], "LastEvaluatedKey": {"k": 1}}
                return {"Items": [3]}

        table = Table()
        assert configdb.query_all(table, KeyConditionExpression="x") == [1, 2, 3]
        assert len(table.calls) == 2 and table.calls[1]["ExclusiveStartKey"] == {"k": 1}

    def test_table_name_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("TELEMETRY_CONFIG_TABLE", "cfg")
        assert configdb.table_name() == "cfg"
