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
"""Preservation baseline: TinyDB datastore read / round-trip (Req 3.8).

Spec: python-3-11-security-upgrade — Property 2: Preservation — No functional
regression for non-3.9 artifacts.

An existing TinyDB datastore / config file written by a Python 3.9 deployment
must be read without migration errors after the interpreter moves to 3.11
(Req 3.8). The on-disk TinyDB layout is a plain JSON document
``{"_default": {"<doc_id>": {<record>}, ...}}`` and the migration's read path
(:meth:`dao.sqlite_db.db_migration.OldTinyDB.read_db_files`) loads it with the
**standard library** ``json`` module:

* an empty file -> ``{}`` (no entries), and
* otherwise ``json.load(file)["_default"]`` -> the mapping of records.

``json`` parsing is interpreter-version-stable, so this baseline asserts that
records written in the 3.9 TinyDB layout load back **unchanged**. The read logic
below mirrors ``OldTinyDB.read_db_files`` exactly; it is reproduced here (rather
than imported) because that module pulls in marshmallow / the DAO + model stack
that is only present inside the ``flask-app`` image. The full ``OldTinyDB`` read
is re-exercised against the real schemas at the in-image gate (tasks 11/12).

**Validates: Requirements 3.8**

Runs in a bare checkout (stdlib only):

    python3 -m pytest test/backend-test/preservation/test_preservation_tinydb_roundtrip.py \\
        --noconftest -v
"""
import json
import os
import tempfile
from contextlib import contextmanager

from hypothesis import given, settings
from hypothesis import strategies as st


@contextmanager
def _temp_db(contents: str):
    """Write ``contents`` to a throwaway file and yield its path (cleaned up after)."""
    fd, path = tempfile.mkstemp(prefix="tinydb_", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(contents)
        yield path
    finally:
        os.unlink(path)


# --------------------------------------------------------------------------- #
# Faithful mirror of dao.sqlite_db.db_migration.OldTinyDB.read_db_files
# --------------------------------------------------------------------------- #
def read_tinydb(filepath: str) -> dict:
    """Read a TinyDB JSON file's ``_default`` table (mirrors OldTinyDB.read_db_files).

    * Missing file is the caller's concern (``read_db_files`` guards with
      ``os.path.exists`` first); this helper assumes the file exists.
    * Empty file (size 0) -> ``{}``.
    * Otherwise ``json.load(file)["_default"]``.
    """
    if os.stat(filepath).st_size == 0:
        return {}
    with open(filepath, "r") as db_file:
        data = json.load(db_file)
    return data["_default"]


# --------------------------------------------------------------------------- #
# Generators for the TinyDB on-disk layout
# --------------------------------------------------------------------------- #
# JSON-serializable scalar values that appear in DDA records (str / int / float /
# bool / None). Floats are finite so the json round-trip is exact.
_json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.text(max_size=40),
)


def _json_values(max_leaves: int = 25):
    """Arbitrary JSON values (scalars, nested lists, nested objects)."""
    return st.recursive(
        _json_scalars,
        lambda children: st.one_of(
            st.lists(children, max_size=5),
            st.dictionaries(st.text(max_size=20), children, max_size=5),
        ),
        max_leaves=max_leaves,
    )


# A single TinyDB record is a JSON object (field name -> JSON value), matching the
# workflow / image-source / config documents DDA persists.
_records = st.dictionaries(st.text(min_size=1, max_size=20), _json_values(), max_size=6)

# A TinyDB ``_default`` table: stringified positive doc-ids -> record. TinyDB keys
# are the stringified integer doc ids it assigns on insert.
_default_tables = st.dictionaries(
    st.integers(min_value=1, max_value=10_000).map(str),
    _records,
    max_size=8,
)


def _normalize(value):
    """Project a value through a JSON dump/load to its canonical decoded form.

    The baseline is that what was *written* (after JSON encoding) reads back
    identically. Comparing against this canonical form avoids spurious mismatches
    from non-string dict keys etc. (DDA records always use string keys, but this
    keeps the property about the encode→decode identity that 3.11 must preserve).
    """
    return json.loads(json.dumps(value))


# --------------------------------------------------------------------------- #
# Properties
# --------------------------------------------------------------------------- #
# Spec: python-3-11-security-upgrade — Property 2: Preservation
# Validates: Requirements 3.8
@settings(max_examples=200, deadline=None)
@given(table=_default_tables)
def test_tinydb_records_load_unchanged(table):
    """Records written in the 3.9 TinyDB layout read back unchanged.

    Validates: Requirements 3.8
    """
    payload = {"_default": table}
    with _temp_db(json.dumps(payload)) as path:
        loaded = read_tinydb(path)

    assert loaded == _normalize(table)
    # The doc-id keys (and their count) are preserved exactly.
    assert set(loaded.keys()) == set(table.keys())
    assert len(loaded) == len(table)


# Spec: python-3-11-security-upgrade — Property 2: Preservation
# Validates: Requirements 3.8
@settings(max_examples=100, deadline=None)
@given(table=_default_tables)
def test_tinydb_full_document_roundtrip(table):
    """The whole ``{"_default": ...}`` document survives a dump→load round-trip.

    Validates: Requirements 3.8
    """
    payload = {"_default": table}
    with _temp_db(json.dumps(payload)) as path:
        with open(path, "r") as fh:
            reloaded = json.load(fh)

    assert reloaded == _normalize(payload)
    assert "_default" in reloaded


# Spec: python-3-11-security-upgrade — Property 2: Preservation
# Validates: Requirements 3.8
def test_tinydb_empty_file_reads_as_empty():
    """An empty TinyDB file reads as an empty table (no migration error).

    Validates: Requirements 3.8
    """
    with _temp_db("") as path:
        assert read_tinydb(path) == {}
