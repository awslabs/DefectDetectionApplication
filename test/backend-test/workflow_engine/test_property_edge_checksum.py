#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Property-based test for LocalServer edge plugin checksum verification.

**Feature: custom-node-designer, Property 9: Edge checksum verification
gates plugin loading**

For all plugin file contents and manifest checksum entries, LocalServer's
plugin loader accepts the file if and only if the SHA-256 of the delivered
bytes equals the manifest checksum, and every rejection identifies the
failing plugin file.

**Validates: Requirements 10.6**

Two instantiations of the property:

1. The pure verification decision (``verify_plugin_checksums``): for any
   manifest ``pluginChecksums`` map and any file-bytes world (correct
   bytes, tampered bytes, missing files, non-string recorded checksums),
   the outcome partitions the entries exactly — a key is verified iff its
   delivered bytes hash to the recorded checksum, and every failure names
   the exact offending key.

2. Loading is gated on that decision (``workflow_plugin_path`` with the
   registry scan mocked, per the existing test patterns): the inline
   plugin directory is scanned iff every checksum entry resolving into it
   verifies; any failure skips the directory (fail closed) and the prior
   ``GST_PLUGIN_PATH`` is always restored.
"""
import hashlib
import os
import tempfile
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine import gst_plugins

ARCH = "x86_64"

# Entry states cover the whole input space of one pluginChecksums entry:
#   ok        — delivered bytes hash to the recorded checksum
#   tampered  — delivered bytes differ from the checksummed bytes
#   missing   — no file bytes are delivered for the key
#   nonstring — the recorded checksum is not a string
#   empty     — the recorded checksum is an empty string
STATES = ("ok", "tampered", "missing", "nonstring", "empty")

_names = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=12
).map(lambda s: s + ".so")

# key -> (state, original bytes); unique keys via dictionaries.
_worlds = st.dictionaries(
    keys=_names,
    values=st.tuples(st.sampled_from(STATES), st.binary(max_size=64)),
    min_size=1,
    max_size=6,
)


def _build_manifest_and_bytes(world):
    """Turn a generated world into (manifest, delivered-bytes map).

    Returns ``checksums`` (the manifest's pluginChecksums), ``contents``
    (key -> delivered bytes, absent when missing) and ``expected_ok``
    (the keys whose delivered bytes hash to the recorded checksum).
    """
    checksums = {}
    contents = {}
    expected_ok = set()
    for key, (state, data) in world.items():
        good_sha = hashlib.sha256(data).hexdigest()
        if state == "ok":
            checksums[key] = good_sha
            contents[key] = data
            expected_ok.add(key)
        elif state == "tampered":
            checksums[key] = good_sha
            # Appending a byte guarantees a different SHA-256 preimage.
            contents[key] = data + b"\x00"
        elif state == "missing":
            checksums[key] = good_sha
        elif state == "nonstring":
            checksums[key] = 12345
            contents[key] = data
        else:  # empty checksum string
            checksums[key] = ""
            contents[key] = data
    return checksums, contents, expected_ok


class TestProperty9EdgeChecksumVerification:
    """**Feature: custom-node-designer, Property 9: Edge checksum
    verification gates plugin loading**

    **Validates: Requirements 10.6**
    """

    @settings(max_examples=25, deadline=None)
    @given(world=_worlds)
    def test_verification_partitions_entries_exactly(self, world):
        """A key is verified iff its bytes hash to the recorded checksum,
        and every failure identifies the exact failing file key."""
        checksums, contents, expected_ok = _build_manifest_and_bytes(world)
        manifest = {"targetArch": ARCH, "pluginChecksums": checksums}

        outcome = gst_plugins.verify_plugin_checksums(
            manifest, lambda key: contents.get(key)
        )

        verified = set(outcome.verified)
        failed = {key for key, _ in outcome.failures}

        # Accept iff SHA-256(delivered bytes) == recorded checksum.
        assert verified == expected_ok
        # Every rejection identifies the exact failing plugin file.
        assert failed == set(checksums) - expected_ok
        # Exact partition: disjoint, jointly exhaustive, no phantom keys.
        assert verified.isdisjoint(failed)
        assert verified | failed == set(checksums)
        assert len(outcome.failures) == len(failed)  # one failure per key
        # Every failure carries a non-empty reason for the status path.
        assert all(reason for _, reason in outcome.failures)
        # ok is the aggregate gate the loader consumes.
        assert outcome.ok == (not failed)

    @settings(max_examples=25, deadline=None)
    @given(
        world=st.dictionaries(
            keys=_names,
            values=st.tuples(
                st.sampled_from(("ok", "tampered", "missing")),
                st.binary(max_size=64),
            ),
            min_size=1,
            max_size=4,
        )
    )
    def test_loading_is_gated_on_verification(self, world):
        """The inline plugin directory joins the run's scan path iff every
        checksum entry verifies; any failure skips it before the registry
        scan (fail closed), and GST_PLUGIN_PATH is always restored."""
        checksums, contents, expected_ok = _build_manifest_and_bytes(world)
        manifest = {"targetArch": ARCH, "pluginChecksums": checksums}
        all_verified = expected_ok == set(checksums)

        with tempfile.TemporaryDirectory() as artifact:
            inline_dir = os.path.join(artifact, "plugins", ARCH)
            for key, data in contents.items():
                os.makedirs(inline_dir, exist_ok=True)
                with open(os.path.join(inline_dir, key), "wb") as f:
                    f.write(data)

            env_before = os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV)
            with patch.object(
                gst_plugins, "_scan_registry", return_value=True
            ) as scan:
                with gst_plugins.workflow_plugin_path(
                    inline_dir,
                    manifest=manifest,
                    artifact_path=artifact,
                ) as applied:
                    if all_verified:
                        # min_size=1 and all ok => files were written, the
                        # directory exists and must be applied and scanned.
                        assert applied
                        env = os.environ[gst_plugins.GST_PLUGIN_PATH_ENV]
                        assert env.split(":")[0] == inline_dir
                    else:
                        # Any failing entry skips the directory: never
                        # applied, never scanned (fail closed, 10.6).
                        assert not applied

            scanned = [call.args[0] for call in scan.call_args_list]
            if all_verified:
                assert scanned == [inline_dir]
            else:
                assert inline_dir not in scanned
            # No lasting environment mutation either way.
            assert (
                os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV) == env_before
            )
