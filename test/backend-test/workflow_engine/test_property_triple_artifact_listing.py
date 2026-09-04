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
"""Property test for the additive per-Inspection run artifacts (task 1.2).

**Feature: imts-triple-inspection-hmi, Property 18: Additive inventory
listing and resolution**

*For any* run artifact directory containing node-frame files over arbitrary
nodeIds and ports (including the new ``original`` and ``annotated`` ports
written by ``BedrockInferenceProcessor``), the results inventory
(``run_artifacts.list_node_images``) lists exactly one node entry per
artifact file, deterministically ordered (nodeId ascending; ``in`` before
``reference`` before other ports alphabetically); every listed
(``nodeId``, ``port``) pair resolves to its own file through
``run_artifacts.node_image_path``; and for any directory containing no
``original``/``annotated`` artifacts the listing is identical to the
pre-change behavior — no existing entry, field, or ordering changes.

**Validates: Requirements 4.4**

The new-port filenames are produced through the very templates the
processor writes (``ORIGINAL_FRAME_ARTIFACT_TEMPLATE`` /
``ANNOTATED_FRAME_ARTIFACT_TEMPLATE`` +
``sanitize_node_id_for_artifact``), so the round trip
"persisted filename -> listed (nodeId, port) -> resolved path" is what is
actually asserted. ``run_artifacts.py`` is exercised unchanged: the tmp-dir
artifact fixture pattern comes from ``test_workflow_run_results_api.py``.
"""
import os
import shutil
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine import run_artifacts
from workflow_engine.output_bindings import (
    ANNOTATED_FRAME_ARTIFACT_TEMPLATE,
    ORIGINAL_FRAME_ARTIFACT_TEMPLATE,
    sanitize_node_id_for_artifact,
)

_CAPTURE_ID = "wf-1-exec-1"
_OTHER_CAPTURE_ID = "other-capture"

#: The additive ports this feature introduces; everything else in the
#: generated inventory is a port the pre-change backend already produced.
_NEW_PORTS = ("original", "annotated")

#: Presentation order ``run_artifacts`` guarantees for known ports; any
#: other port sorts after these, alphabetically. Re-stated here as an
#: independent oracle rather than importing the module's private key.
_KNOWN_PORT_ORDER = ("in", "reference")


def _expected_sort_key(entry):
    port = entry["port"]
    if port in _KNOWN_PORT_ORDER:
        return (entry["nodeId"], _KNOWN_PORT_ORDER.index(port), "")
    return (entry["nodeId"], len(_KNOWN_PORT_ORDER), port)


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #

#: Raw node ids as a Workflow_Definition may carry them: safe characters,
#: dots (kept by the sanitizer, so the ``{nodeId}.{port}`` tail must still
#: split on its LAST dot), and characters the sanitizer replaces.
_RAW_NODE_IDS = st.text(
    alphabet=st.sampled_from(list("abAB01_-. /:#") + ["é"]),
    min_size=0,
    max_size=10,
)

#: Port names: the two known ports, the two additive ones, and arbitrary
#: dot-free port names (the listing carries no port-name allow-list).
_PORTS = st.one_of(
    st.sampled_from(["in", "reference", "original", "annotated"]),
    st.text(
        alphabet=st.sampled_from(list("abAB01_-")),
        min_size=1,
        max_size=6,
    ),
)


@st.composite
def _inventories(draw):
    """A list of unique (``safe_node_id``, ``port``) node-frame artifacts.

    Distinct raw node ids can sanitize to the same safe id, and the same
    (safe id, port) pair is one single artifact file, so the drawn pairs
    are de-duplicated on the sanitized form the filename actually uses.
    """
    pairs = draw(
        st.lists(st.tuples(_RAW_NODE_IDS, _PORTS), min_size=0, max_size=8)
    )
    seen = set()
    inventory = []
    for raw_node_id, port in pairs:
        safe_node_id = sanitize_node_id_for_artifact(raw_node_id)
        key = (safe_node_id, port)
        if key in seen:
            continue
        seen.add(key)
        inventory.append({"nodeId": safe_node_id, "port": port})
    return inventory


# --------------------------------------------------------------------------- #
# Artifact fixture: a per-run output_dir of capture_id-prefixed files
# (pattern from test_workflow_run_results_api.py).
# --------------------------------------------------------------------------- #


def _write(path, data):
    with open(path, "wb" if isinstance(data, bytes) else "w") as artifact:
        artifact.write(data)


def _node_artifact_name(node_id, port):
    """The filename a node frame for (``node_id``, ``port``) lands under.

    The additive ports go through the processor's own templates so this
    test asserts the real persisted names, not a restatement of them.
    """
    if port == "original":
        return ORIGINAL_FRAME_ARTIFACT_TEMPLATE.format(
            capture_id=_CAPTURE_ID, safe_node_id=node_id
        )
    if port == "annotated":
        return ANNOTATED_FRAME_ARTIFACT_TEMPLATE.format(
            capture_id=_CAPTURE_ID, safe_node_id=node_id
        )
    return "{0}.node.{1}.{2}.jpg".format(_CAPTURE_ID, node_id, port)


def _expected_bytes(node_id, port):
    return "frame::{0}::{1}".format(node_id, port).encode()


def _seed_artifact_dir(inventory):
    """A fresh run ``output_dir`` holding one file per inventory entry plus
    the non-node artifacts and malformed/foreign names a real run leaves
    around (all of which the listing must ignore)."""
    out = tempfile.mkdtemp(prefix="triple-artifacts-")
    for entry in inventory:
        _write(
            os.path.join(out, _node_artifact_name(entry["nodeId"], entry["port"])),
            _expected_bytes(entry["nodeId"], entry["port"]),
        )
    # Non-node run artifacts, another run's node frame, a node frame with no
    # port, and a non-jpg node frame: none of these are node entries.
    _write(os.path.join(out, "{0}.jpg".format(_CAPTURE_ID)), b"base")
    _write(os.path.join(out, "{0}.overlay.jpg".format(_CAPTURE_ID)), b"overlay")
    _write(os.path.join(out, "{0}.json".format(_CAPTURE_ID)), "{}")
    _write(
        os.path.join(out, "{0}.node.vlm1.in.jpg".format(_OTHER_CAPTURE_ID)),
        b"other-run",
    )
    _write(os.path.join(out, "{0}.node.noport.jpg".format(_CAPTURE_ID)), b"x")
    _write(os.path.join(out, "{0}.node.n1.in.png".format(_CAPTURE_ID)), b"x")
    return out


# --------------------------------------------------------------------------- #
# Feature: imts-triple-inspection-hmi, Property 18: Additive inventory
# listing and resolution
# --------------------------------------------------------------------------- #


@settings(max_examples=100, deadline=None)
@given(inventory=_inventories())
def test_property_18_every_node_artifact_is_listed_once_and_resolves(inventory):
    """**Feature: imts-triple-inspection-hmi, Property 18: Additive
    inventory listing and resolution**

    **Validates: Requirements 4.4**

    Exactly one entry per node-frame artifact file (including the new
    ``original``/``annotated`` ports), deterministically ordered, and every
    listed pair resolves to its own file's bytes.
    """
    out = _seed_artifact_dir(inventory)
    try:
        listed = run_artifacts.list_node_images(out, _CAPTURE_ID)

        # Exactly one entry per artifact file, no extras, fields unchanged.
        assert all(set(entry) == {"nodeId", "port"} for entry in listed)
        assert len(listed) == len(inventory)
        assert sorted(
            (e["nodeId"], e["port"]) for e in listed
        ) == sorted((e["nodeId"], e["port"]) for e in inventory)

        # Deterministic order: nodeId ascending, then in < reference <
        # other ports alphabetically.
        assert listed == sorted(listed, key=_expected_sort_key)

        # Every listed pair resolves to its OWN file (no substitution
        # across nodes or ports) through the unchanged resolver.
        for entry in listed:
            path = run_artifacts.node_image_path(
                out, _CAPTURE_ID, entry["nodeId"], entry["port"]
            )
            assert path is not None
            assert os.path.basename(path) == _node_artifact_name(
                entry["nodeId"], entry["port"]
            )
            with open(path, "rb") as artifact:
                assert artifact.read() == _expected_bytes(
                    entry["nodeId"], entry["port"]
                )
    finally:
        shutil.rmtree(out, ignore_errors=True)


@settings(max_examples=100, deadline=None)
@given(inventory=_inventories())
def test_property_18_new_ports_leave_pre_change_listing_identical(inventory):
    """**Feature: imts-triple-inspection-hmi, Property 18: Additive
    inventory listing and resolution**

    **Validates: Requirements 4.4**

    A directory holding only pre-change node frames lists exactly what it
    lists today, and adding the ``original``/``annotated`` artifacts leaves
    every pre-existing entry, field, and their ordering untouched — the new
    entries are purely additive.
    """
    legacy = [e for e in inventory if e["port"] not in _NEW_PORTS]

    with_new = _seed_artifact_dir(inventory)
    legacy_only = _seed_artifact_dir(legacy)
    try:
        listed_with_new = run_artifacts.list_node_images(with_new, _CAPTURE_ID)
        listed_legacy = run_artifacts.list_node_images(legacy_only, _CAPTURE_ID)

        # Pre-change behavior on a directory with no new artifacts.
        assert listed_legacy == sorted(legacy, key=_expected_sort_key)

        # The additive artifacts add entries and change nothing else:
        # dropping the new ports from the full listing reproduces the
        # pre-change listing exactly, order included.
        assert [
            e for e in listed_with_new if e["port"] not in _NEW_PORTS
        ] == listed_legacy

        # Pre-change pairs keep resolving, and the new ports never resolve
        # in a directory that has none of them.
        for entry in listed_legacy:
            assert (
                run_artifacts.node_image_path(
                    legacy_only, _CAPTURE_ID, entry["nodeId"], entry["port"]
                )
                is not None
            )
            for new_port in _NEW_PORTS:
                assert (
                    run_artifacts.node_image_path(
                        legacy_only, _CAPTURE_ID, entry["nodeId"], new_port
                    )
                    is None
                )
    finally:
        shutil.rmtree(with_new, ignore_errors=True)
        shutil.rmtree(legacy_only, ignore_errors=True)
