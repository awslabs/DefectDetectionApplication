"""``workflow_debug.log_filters`` (workflow-debug-mode design §7.2; tasks.md
3.4): every server-side filter, the answer-event shape, and ``next_token`` on
a same-token ``GetLogEvents`` page, an empty page, a new token, and a
``FilterLogEvents`` response without ``nextToken``.
"""
import json

import pytest

from workflow_debug import log_filters as lf


def cw_event(record, timestamp=None):
    return {"timestamp": timestamp or record.get("writeTs", 0), "message": json.dumps(record),
            "ingestionTime": 1}


RUN = {"type": "run", "ts": 1000, "id": "s:i:1:0", "writeTs": 5000, "receivedMs": 4000,
       "executionId": "e-1", "status": "failed",
       "nodes": [{"nodeId": "detector", "status": "completed"}, {"nodeId": "publish", "status": "failed"}]}
LOG_INFO = {"type": "log", "ts": 1500, "id": "s:i:1:1", "writeTs": 5000, "receivedMs": 4000,
            "level": "INFO", "logger": "workflow_engine", "message": "between runs"}
LOG_ERROR = {"type": "log", "ts": 2000, "id": "s:i:1:2", "writeTs": 5000, "receivedMs": 4000,
             "level": "error", "logger": "dda_triton", "message": "readiness"}
DROP = {"type": "drop", "ts": 2500, "id": "s:i:2:0", "writeTs": 5001, "receivedMs": 4001,
        "nodeId": "publish", "reason": "queue_full", "count": 3}
SESSION = {"type": "session", "ts": 3000, "id": "s:i:2:1", "writeTs": 5001, "receivedMs": 4001,
           "state": "active", "reason": "heartbeat"}
ALL = [RUN, LOG_INFO, LOG_ERROR, DROP, SESSION]


@pytest.fixture
def events():
    return lf.to_answer_events(cw_event(r) for r in ALL)


def ids(events):
    return [e["id"] for e in events]


class TestAnswerShape:
    def test_fields_come_from_the_record(self, events):
        first = events[0]
        assert set(first) == {"id", "writeTs", "receivedMs", "ts", "record"}
        assert first["id"] == "s:i:1:0" and first["writeTs"] == 5000 and first["receivedMs"] == 4000
        assert first["ts"] == 1000 and first["record"] == RUN

    def test_non_json_message_is_shown_raw(self):
        answer = lf.to_answer_event({"timestamp": 77, "message": "plain text"})
        assert answer["record"] == {"type": "raw", "message": "plain text"}
        assert answer["writeTs"] == 77 and answer["id"] is None and answer["ts"] is None

    def test_json_that_is_not_an_object_is_raw(self):
        answer = lf.to_answer_event({"timestamp": 77, "message": "[1, 2]"})
        assert answer["record"]["type"] == "raw"

    def test_missing_write_ts_falls_back_to_the_event_timestamp(self):
        answer = lf.to_answer_event({"timestamp": 88, "message": json.dumps({"type": "log", "ts": 1})})
        assert answer["writeTs"] == 88


class TestApply:
    def test_no_filter_keeps_everything(self, events):
        assert lf.apply(events) == events

    def test_type(self, events):
        assert ids(lf.apply(events, type="log")) == ["s:i:1:1", "s:i:1:2"]
        assert ids(lf.apply(events, type="run")) == ["s:i:1:0"]
        assert lf.apply(events, type="nope") == []

    def test_level_is_case_insensitive_and_excludes_records_without_one(self, events):
        assert ids(lf.apply(events, level="ERROR")) == ["s:i:1:2"]
        assert ids(lf.apply(events, level="info")) == ["s:i:1:1"]
        assert lf.apply(events, level="DEBUG") == []

    def test_node_id_on_nodes_and_on_a_drop_records_own_node(self, events):
        assert ids(lf.apply(events, node_id="publish")) == ["s:i:1:0", "s:i:2:0"]
        assert ids(lf.apply(events, node_id="detector")) == ["s:i:1:0"]
        assert lf.apply(events, node_id="missing") == []

    def test_ts_window_is_inclusive_on_the_records_own_ts(self, events):
        assert ids(lf.apply(events, ts_from=1500, ts_to=2500)) == ["s:i:1:1", "s:i:1:2", "s:i:2:0"]
        assert ids(lf.apply(events, ts_from=3000)) == ["s:i:2:1"]
        assert ids(lf.apply(events, ts_to=1000)) == ["s:i:1:0"]

    def test_records_without_an_integer_ts_fall_out_of_a_ts_window(self):
        events = lf.to_answer_events([cw_event({"type": "raw-ish", "writeTs": 1}),
                                      {"timestamp": 2, "message": "plain"}])
        assert lf.apply(events, ts_from=0) == []
        assert len(lf.apply(events)) == 2

    def test_filters_combine(self, events):
        assert ids(lf.apply(events, type="log", level="INFO", ts_from=0, ts_to=9999)) == ["s:i:1:1"]
        assert lf.apply(events, type="log", node_id="publish") == []


class TestNextToken:
    def test_get_log_events_same_token_is_the_end(self):
        response = {"events": [{"timestamp": 1, "message": "{}"}], "nextForwardToken": "f/abc"}
        assert lf.next_token("f/abc", response, "get") is None

    def test_get_log_events_empty_page_is_the_end(self):
        response = {"events": [], "nextForwardToken": "f/new"}
        assert lf.next_token("f/old", response, "get") is None
        assert lf.next_token(None, response, "get") is None

    def test_get_log_events_new_token_continues(self):
        response = {"events": [{"timestamp": 1, "message": "{}"}], "nextForwardToken": "f/new"}
        assert lf.next_token("f/old", response, "get") == "f/new"
        assert lf.next_token(None, response, "get") == "f/new"

    def test_get_log_events_without_a_token_is_the_end(self):
        assert lf.next_token(None, {"events": [{"timestamp": 1, "message": "{}"}]}, "get") is None

    def test_filter_log_events_without_next_token_is_the_end(self):
        assert lf.next_token(None, {"events": [{"timestamp": 1, "message": "{}"}]}, "filter") is None
        assert lf.next_token("t", {"events": [], "nextToken": ""}, "filter") is None

    def test_filter_log_events_token_continues(self):
        assert lf.next_token(None, {"events": [], "nextToken": "n/1"}, "filter") == "n/1"
        assert lf.next_token("n/1", {"events": [], "nextToken": "n/1"}, "filter") == "n/1"

    def test_unknown_api(self):
        with pytest.raises(ValueError):
            lf.next_token(None, {}, "describe")
