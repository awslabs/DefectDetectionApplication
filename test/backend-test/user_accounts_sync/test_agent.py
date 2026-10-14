# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit tests for the user-accounts edge sync agent (task 10.1).

Example-based tests with fakes covering:

- ``parse_sync_document``: valid documents parse with full record
  preservation, deleted accounts normalized to ``enabled: false`` and
  never dropped (7.8); shape violations raise ``SyncDocumentError``,
- atomic cache replacement: file mode ``0600``, full-set replacement (7.1),
- the ack protocol: success writes ``{ackSyncId, appliedAt, accountCount}``,
  validation failure writes ``{ackSyncId, error}`` with the existing cache
  untouched (7.4),
- startup catch-up from ``GetThingShadow`` and its already-acked skip,
- crash isolation: shadow/transport failures never propagate.

_Requirements: 7.1, 7.4, 7.8, 7.9_
"""
import json
import os
import stat

import pytest

from user_accounts_sync import (
    SyncDocumentError,
    UserAccountsSyncAgent,
    build_cache_document,
    delta_topic_prefix,
    parse_sync_document,
    write_cache_atomically,
)

_VERIFIER = {
    "algorithm": "pbkdf2-sha256",
    "iterations": 210000,
    "salt": "c2FsdA==",
    "hash": "aGFzaA==",
}


def _document(sync_id="sync-1", accounts=None, version=1):
    if accounts is None:
        accounts = {
            "operator1": {
                "email": "op1@example.com",
                "role": "Operator",
                "enabled": True,
                "verifier": dict(_VERIFIER),
            },
            "olduser": {"email": "x@y.z", "role": "Viewer", "enabled": False},
        }
    return {"syncId": sync_id, "version": version, "accounts": accounts}


class _FakeShadow:
    """Fake IoTShadowAccessor recording reported writes."""

    def __init__(self, state=False):
        self.state = state  # False mirrors the not-found contract
        self.updates = []
        self.get_error = None
        self.update_error = None

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        if self.get_error is not None:
            raise self.get_error
        return self.state

    def update_thing_shadow_state_request(self, thing_name, shadow_name, payload):
        if self.update_error is not None:
            raise self.update_error
        self.updates.append((thing_name, shadow_name, payload))
        return b"{}"


def _make_agent(tmp_path, shadow=None):
    shadow = shadow if shadow is not None else _FakeShadow()
    agent = UserAccountsSyncAgent(
        shadow,
        thing_name="test-thing",
        cache_path=str(tmp_path / "local_credential_cache.json"),
        wall_clock=lambda: 1700.0,  # appliedAt == 1700000
    )
    return agent, shadow


def _read_cache(agent):
    with open(agent.cache_path) as handle:
        return json.load(handle)


# --- parse_sync_document -------------------------------------------------------


class TestParseSyncDocument:
    def test_valid_document_parses_with_records_preserved(self):
        sync_id, accounts = parse_sync_document(_document())
        assert sync_id == "sync-1"
        assert set(accounts) == {"operator1", "olduser"}
        assert accounts["operator1"]["verifier"] == _VERIFIER
        assert accounts["operator1"]["enabled"] is True
        assert accounts["olduser"] == {
            "email": "x@y.z",
            "role": "Viewer",
            "enabled": False,
        }

    def test_deleted_account_marked_disabled_never_dropped(self):
        # Requirement 7.8: deleted accounts stay in the set, disabled.
        document = _document(
            accounts={
                "gone": {
                    "email": "gone@example.com",
                    "role": "Viewer",
                    "enabled": True,
                    "deleted": True,
                }
            }
        )
        _, accounts = parse_sync_document(document)
        assert "gone" in accounts
        assert accounts["gone"]["enabled"] is False
        assert accounts["gone"]["deleted"] is True

    def test_empty_account_set_is_valid(self):
        sync_id, accounts = parse_sync_document(_document(accounts={}))
        assert sync_id == "sync-1"
        assert accounts == {}

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.pop("syncId"),
            lambda d: d.update(syncId=""),
            lambda d: d.update(version=2),
            lambda d: d.update(version=True),
            lambda d: d.pop("accounts"),
            lambda d: d.update(accounts=[]),
            lambda d: d["accounts"].update(bad="not-an-object"),
            lambda d: d["accounts"]["operator1"].pop("email"),
            lambda d: d["accounts"]["operator1"].update(role=""),
            lambda d: d["accounts"]["operator1"].update(enabled="yes"),
            lambda d: d["accounts"]["operator1"].update(deleted="yes"),
            lambda d: d["accounts"]["operator1"]["verifier"].pop("salt"),
            lambda d: d["accounts"]["operator1"]["verifier"].update(iterations=0),
        ],
    )
    def test_shape_violations_raise(self, mutate):
        document = _document()
        mutate(document)
        with pytest.raises(SyncDocumentError):
            parse_sync_document(document)

    def test_non_mapping_document_raises(self):
        with pytest.raises(SyncDocumentError):
            parse_sync_document("not a document")


# --- atomic cache replacement ---------------------------------------------------


class TestCacheReplacement:
    def test_write_creates_file_with_0600_mode(self, tmp_path):
        path = str(tmp_path / "cache.json")
        write_cache_atomically(build_cache_document("s1", {}, 1), path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600
        with open(path) as handle:
            assert json.load(handle) == {
                "version": 1,
                "syncId": "s1",
                "appliedAt": 1,
                "accounts": {},
            }

    def test_replacement_is_full_set_and_leaves_no_temp_files(self, tmp_path):
        path = str(tmp_path / "cache.json")
        write_cache_atomically(
            build_cache_document("s1", {"a": {"enabled": True}}, 1), path
        )
        write_cache_atomically(
            build_cache_document("s2", {"b": {"enabled": False}}, 2), path
        )
        with open(path) as handle:
            cache = json.load(handle)
        assert cache["syncId"] == "s2"
        assert set(cache["accounts"]) == {"b"}  # full replacement, no merge
        assert os.listdir(str(tmp_path)) == ["cache.json"]


# --- apply + ack protocol -------------------------------------------------------


class TestApplySyncDocument:
    def test_valid_document_replaces_cache_and_acks(self, tmp_path):
        agent, shadow = _make_agent(tmp_path)
        agent.apply_sync_document(_document())

        cache = _read_cache(agent)
        assert cache["version"] == 1
        assert cache["syncId"] == "sync-1"
        assert cache["appliedAt"] == 1700000
        assert set(cache["accounts"]) == {"operator1", "olduser"}
        assert cache["accounts"]["operator1"]["verifier"] == _VERIFIER

        assert len(shadow.updates) == 1
        thing, name, payload = shadow.updates[0]
        assert (thing, name) == ("test-thing", "dda-user-accounts")
        reported = payload["reported"]
        assert reported["ackSyncId"] == "sync-1"
        assert reported["appliedAt"] == 1700000
        assert reported["accountCount"] == 2
        assert reported["error"] is None

    def test_invalid_document_acks_error_and_keeps_cache(self, tmp_path):
        agent, shadow = _make_agent(tmp_path)
        agent.apply_sync_document(_document(sync_id="good"))
        before = _read_cache(agent)

        bad = _document(sync_id="bad")
        bad["accounts"]["operator1"]["enabled"] = "yes"
        agent.apply_sync_document(bad)

        assert _read_cache(agent) == before  # existing cache untouched
        reported = shadow.updates[-1][2]["reported"]
        assert reported["ackSyncId"] == "bad"
        assert "enabled" in reported["error"]
        assert reported["appliedAt"] is None
        assert reported["accountCount"] is None

    def test_delta_message_unwraps_state(self, tmp_path):
        agent, shadow = _make_agent(tmp_path)
        agent.on_delta({"state": _document(sync_id="delta-1"), "version": 7})
        assert _read_cache(agent)["syncId"] == "delta-1"
        assert shadow.updates[-1][2]["reported"]["ackSyncId"] == "delta-1"

    def test_ack_write_failure_never_raises(self, tmp_path):
        agent, shadow = _make_agent(tmp_path)
        shadow.update_error = RuntimeError("offline")
        agent.apply_sync_document(_document())
        # Cache applied; the lost ack is the portal timeout's problem (7.9).
        assert _read_cache(agent)["syncId"] == "sync-1"


# --- startup catch-up -----------------------------------------------------------


class TestStartupCatchUp:
    def test_start_applies_pending_desired_state(self, tmp_path):
        shadow = _FakeShadow(state={"desired": _document(sync_id="offline-1")})
        agent, _ = _make_agent(tmp_path, shadow)
        agent.start()
        assert _read_cache(agent)["syncId"] == "offline-1"
        assert shadow.updates[-1][2]["reported"]["ackSyncId"] == "offline-1"

    def test_start_skips_already_acked_and_applied_sync(self, tmp_path):
        shadow = _FakeShadow(
            state={
                "desired": _document(sync_id="done-1"),
                "reported": {"ackSyncId": "done-1"},
            }
        )
        agent, _ = _make_agent(tmp_path, shadow)
        write_cache_atomically(build_cache_document("done-1", {}, 1), agent.cache_path)
        agent.start()
        assert shadow.updates == []

    def test_start_reapplies_when_cache_lost(self, tmp_path):
        # Acked in the shadow but the cache file is gone: re-apply.
        shadow = _FakeShadow(
            state={
                "desired": _document(sync_id="done-1"),
                "reported": {"ackSyncId": "done-1"},
            }
        )
        agent, _ = _make_agent(tmp_path, shadow)
        agent.start()
        assert _read_cache(agent)["syncId"] == "done-1"

    def test_start_with_missing_shadow_or_error_never_raises(self, tmp_path):
        agent, shadow = _make_agent(tmp_path, _FakeShadow(state=False))
        agent.start()  # shadow does not exist yet
        assert shadow.updates == []

        shadow.get_error = RuntimeError("ipc down")
        agent.start()  # transport error swallowed
        assert shadow.updates == []


# --- topic wiring ---------------------------------------------------------------


def test_delta_topic_prefix():
    assert delta_topic_prefix("thing-a") == (
        "$aws/things/thing-a/shadow/name/dda-user-accounts/update/"
    )
