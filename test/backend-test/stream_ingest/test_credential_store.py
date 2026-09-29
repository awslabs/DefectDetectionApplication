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
"""The Credential_Store (rtsp-rtmp-stream-cameras task 15.5 — Requirement 6.2).

- The directory is 0700 and the file 0600, whatever the umask and whatever
  mode a pre-existing directory had.
- Writes are atomic: no temporary file survives, and a failed replace keeps
  the previous file intact.
- A change made by another process is picked up on the next read.
- A corrupt file reads as empty, and neither the error nor the log carries
  any of its content.
"""
import json
import logging
import os
import stat
import threading

import pytest

from stream_ingest import credentials as credentials_module
from stream_ingest.credentials import (
    DIRECTORY_MODE,
    FILE_MODE,
    CredentialStore,
    CredentialStoreError,
)

PASSWORD = "pw-CRED-5b21"
USERNAME = "user-CRED-9d04"
URL_SECRET = "key-CRED-77ae"


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _temp_files(directory):
    return [name for name in os.listdir(directory) if name.endswith(".tmp")]


@pytest.fixture
def directory(tmp_path):
    return str(tmp_path / "stream_credentials")


@pytest.fixture
def permissive_umask():
    previous = os.umask(0)
    yield
    os.umask(previous)


class TestModes:
    def test_a_new_store_is_0700_and_its_file_0600_under_a_permissive_umask(
            self, directory, permissive_umask):
        store = CredentialStore(directory=directory)
        store.put("src-1", {"password": PASSWORD})

        assert _mode(directory) == DIRECTORY_MODE == 0o700
        assert _mode(store.path) == FILE_MODE == 0o600

    def test_a_pre_existing_open_directory_is_tightened(self, directory):
        os.makedirs(directory)
        os.chmod(directory, 0o755)

        CredentialStore(directory=directory).put("src-1", {"password": PASSWORD})

        assert _mode(directory) == 0o700

    def test_a_pre_existing_open_file_is_replaced_by_a_0600_one(self, directory):
        os.makedirs(directory, mode=0o700)
        path = os.path.join(directory, credentials_module.STORE_FILE_NAME)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "entries": {}}, handle)
        os.chmod(path, 0o644)

        CredentialStore(directory=directory).put("src-1", {"password": PASSWORD})

        assert _mode(path) == 0o600

    def test_the_default_location_is_under_the_component_work_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COMPONENT_WORK_PATH", str(tmp_path))

        store = CredentialStore()

        assert store.path == str(tmp_path / "stream_credentials" / "credentials.json")


class TestAtomicWrites:
    def test_no_temporary_file_survives_a_write(self, directory):
        store = CredentialStore(directory=directory)
        for index in range(5):
            store.put(f"src-{index}", {"password": f"{PASSWORD}-{index}"})
        store.delete("src-0")

        assert _temp_files(directory) == []
        assert sorted(os.listdir(directory)) == ["credentials.json"]

    def test_a_failed_replace_keeps_the_previous_file(self, directory, monkeypatch):
        store = CredentialStore(directory=directory)
        store.put("src-1", {"password": PASSWORD})
        with open(store.path, "rb") as handle:
            before = handle.read()

        def failing_replace(source, destination):
            raise OSError("disk full")

        monkeypatch.setattr(credentials_module.os, "replace", failing_replace)
        with pytest.raises(CredentialStoreError) as raised:
            store.put("src-2", {"password": "never-written-3e1f"})

        with open(store.path, "rb") as handle:
            assert handle.read() == before
        assert _temp_files(directory) == []
        assert "never-written-3e1f" not in str(raised.value)
        # The store still answers from what is on disk.
        assert store.get("src-1") == {"password": PASSWORD}
        assert store.get("src-2") is None

    def test_an_unwritable_directory_is_a_store_error_without_secrets(self, tmp_path):
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("x")

        with pytest.raises(CredentialStoreError) as raised:
            CredentialStore(directory=str(blocker)).put("src-1", {"password": PASSWORD})

        assert PASSWORD not in str(raised.value)

    def test_concurrent_writers_lose_no_entry(self, directory):
        store = CredentialStore(directory=directory)
        errors = []

        def writer(index):
            try:
                store.put(f"src-{index}", {"password": f"pw-{index}"})
            except Exception as error:  # pragma: no cover - reported below
                errors.append(error)

        threads = [threading.Thread(target=writer, args=(index,)) for index in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        reread = CredentialStore(directory=directory)
        assert {f"src-{index}" for index in range(16)} == {
            key for key in json.load(open(reread.path, encoding="utf-8"))["entries"]}
        assert _temp_files(directory) == []


class TestEntries:
    def test_put_get_and_configured(self, directory):
        store = CredentialStore(directory=directory)
        store.put("src-1", {"username": USERNAME, "password": PASSWORD})

        assert store.get("src-1") == {"username": USERNAME, "password": PASSWORD}
        assert store.configured("src-1") is True
        assert store.configured("src-2") is False
        assert store.get("src-2") is None

    def test_get_returns_a_copy(self, directory):
        store = CredentialStore(directory=directory)
        store.put("src-1", {"password": PASSWORD})

        store.get("src-1")["password"] = "tampered"

        assert store.get("src-1") == {"password": PASSWORD}

    def test_unknown_and_empty_fields_are_not_stored(self, directory):
        store = CredentialStore(directory=directory)
        store.put("src-1", {"password": PASSWORD, "username": "", "token": "t-1", "urlSecret": None})

        assert store.get("src-1") == {"password": PASSWORD}
        with open(store.path, encoding="utf-8") as handle:
            assert "t-1" not in handle.read()

    def test_put_without_values_deletes_the_entry(self, directory):
        store = CredentialStore(directory=directory)
        store.put("src-1", {"password": PASSWORD})

        store.put("src-1", {"password": ""})

        assert store.get("src-1") is None

    def test_delete_reports_whether_an_entry_existed(self, directory):
        store = CredentialStore(directory=directory)
        store.put("src-1", {"urlSecret": URL_SECRET})

        assert store.delete("src-1") is True
        assert store.delete("src-1") is False
        assert store.get("src-1") is None

    def test_secret_values_lists_every_stored_value(self, directory):
        store = CredentialStore(directory=directory)
        store.put("src-1", {"username": USERNAME, "password": PASSWORD})
        store.put("src-2", {"urlSecret": URL_SECRET})

        assert sorted(store.secret_values()) == sorted([USERNAME, PASSWORD, URL_SECRET])

    def test_a_missing_file_is_an_empty_store(self, directory):
        store = CredentialStore(directory=directory)

        assert store.get("src-1") is None
        assert store.secret_values() == []
        assert not os.path.exists(directory)

    def test_the_file_holds_only_the_entries(self, directory):
        store = CredentialStore(directory=directory, time_fn=lambda: 1234.5)
        store.put("src-1", {"password": PASSWORD})

        with open(store.path, encoding="utf-8") as handle:
            document = json.load(handle)

        assert document == {"version": 1, "updatedAtMs": 1234500,
                            "entries": {"src-1": {"password": PASSWORD}}}


class TestReload:
    def test_a_change_by_another_writer_is_seen_on_the_next_read(self, directory):
        reader = CredentialStore(directory=directory)
        writer = CredentialStore(directory=directory)
        writer.put("src-1", {"password": PASSWORD})
        assert reader.get("src-1") == {"password": PASSWORD}

        writer.put("src-1", {"password": "rotated-0c9d"})
        writer.put("src-2", {"urlSecret": URL_SECRET})

        assert reader.get("src-1") == {"password": "rotated-0c9d"}
        assert reader.configured("src-2") is True
        assert URL_SECRET in reader.secret_values()

    def test_a_removed_file_empties_the_store(self, directory):
        store = CredentialStore(directory=directory)
        store.put("src-1", {"password": PASSWORD})

        os.unlink(store.path)

        assert store.get("src-1") is None
        assert store.secret_values() == []


class TestCorruptFile:
    @pytest.mark.parametrize("content", [
        "{ not json " + PASSWORD,
        json.dumps(["src-1", PASSWORD]),
        json.dumps({"entries": [PASSWORD]}),
        "\x00\xff" + PASSWORD,
    ])
    def test_a_corrupt_file_reads_as_empty_and_its_content_is_not_logged(
            self, directory, content, caplog):
        os.makedirs(directory, mode=0o700)
        with open(os.path.join(directory, "credentials.json"), "w",
                  encoding="utf-8", errors="surrogateescape") as handle:
            handle.write(content)
        store = CredentialStore(directory=directory)

        with caplog.at_level(logging.DEBUG, logger=credentials_module.__name__):
            assert store.get("src-1") is None
            assert store.secret_values() == []

        assert PASSWORD not in caplog.text

    def test_malformed_entries_are_dropped_and_valid_ones_kept(self, directory):
        os.makedirs(directory, mode=0o700)
        with open(os.path.join(directory, "credentials.json"), "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "entries": {
                "src-1": {"password": PASSWORD, "username": 7},
                "src-2": "not-an-entry",
                "src-3": {"urlSecret": ""},
            }}, handle)
        store = CredentialStore(directory=directory)

        assert store.get("src-1") == {"password": PASSWORD}
        assert store.get("src-2") is None
        assert store.get("src-3") is None

    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                        reason="root reads through any mode")
    def test_an_unreachable_store_is_empty_and_reported_once(self, directory, caplog):
        store = CredentialStore(directory=directory)
        store.put("src-1", {"password": PASSWORD})
        os.chmod(directory, 0)
        try:
            reader = CredentialStore(directory=directory)
            with caplog.at_level(logging.DEBUG, logger=credentials_module.__name__):
                for _ in range(3):
                    assert reader.get("src-1") is None
                    assert reader.secret_values() == []
        finally:
            os.chmod(directory, DIRECTORY_MODE)

        errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert PASSWORD not in caplog.text

    def test_a_write_after_corruption_restores_a_valid_file(self, directory):
        os.makedirs(directory, mode=0o700)
        with open(os.path.join(directory, "credentials.json"), "w", encoding="utf-8") as handle:
            handle.write("{ torn")
        store = CredentialStore(directory=directory)

        store.put("src-1", {"password": PASSWORD})

        assert CredentialStore(directory=directory).get("src-1") == {"password": PASSWORD}
        assert _mode(store.path) == 0o600


class TestProcessWideStore:
    def test_set_and_get(self, directory):
        replacement = CredentialStore(directory=directory)
        credentials_module.set_credential_store(replacement)
        try:
            assert credentials_module.get_credential_store() is replacement
        finally:
            credentials_module.set_credential_store(None)

    def test_the_default_is_created_once(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COMPONENT_WORK_PATH", str(tmp_path))
        credentials_module.set_credential_store(None)
        try:
            first = credentials_module.get_credential_store()
            assert credentials_module.get_credential_store() is first
            assert first.path == str(tmp_path / "stream_credentials" / "credentials.json")
        finally:
            credentials_module.set_credential_store(None)
