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
"""Property tests for ``workflow_engine.detections``
(detection-guided-bedrock-inspection Properties 1 and 2).

Hypothesis-generated raw detection maps (marshal entry shape:
``{"class_index", "class_label", "bounding_box": [x_min, y_min, x_max,
y_max], "confidence"}``) exercise the run-state caching of
``merge_detections`` and the ID assignment of ``build_detection_list``
across every Detection_Sort_Order. The suite's registered hypothesis
profile (``engine-fast`` / ``ci``, see ``conftest.py``) governs example
counts.
"""
import base64
import json
import os
import random
import tempfile

from hypothesis import given
from hypothesis import strategies as st

from workflow_engine import detections

# ---------------------------------------------------------------------------
# Generators: raw marshal detection entries.
# ---------------------------------------------------------------------------

#: Source-frame pixel coordinates (the thor1 fixture is a 4K-ish frame).
_coordinates = st.floats(
    min_value=0.0, max_value=4096.0, allow_nan=False, allow_infinity=False
)
_extents = st.floats(
    min_value=0.0, max_value=1024.0, allow_nan=False, allow_infinity=False
)
_confidences = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
)
_labels = st.sampled_from(["blue box", "plate", "widget", ""])


@st.composite
def raw_detection_entries(draw, max_size=8):
    """A list of raw marshal entries, exactly the decoded ``detections``
    map values (boxes as ``[x_min, y_min, x_max, y_max]`` floats)."""
    size = draw(st.integers(min_value=0, max_value=max_size))
    entries = []
    for _ in range(size):
        x_min = draw(_coordinates)
        y_min = draw(_coordinates)
        entries.append(
            {
                "class_index": 0,
                "class_label": draw(_labels),
                "bounding_box": [
                    x_min,
                    y_min,
                    x_min + draw(_extents),
                    y_min + draw(_extents),
                ],
                "confidence": draw(_confidences),
            }
        )
    return entries


def write_capture_record(output_dir, capture_id, raw_entries):
    """A marshal-shaped capture record at ``{output_dir}/{capture_id}.jsonl``
    whose json_with_base64_encoding block carries ``raw_entries``."""
    payload = {
        "detections": {str(i): entry for i, entry in enumerate(raw_entries)}
    }
    record = {
        "deviceFleetAuxiliaryOutputs": [
            {
                "observedContentType": "json_with_base64_encoding",
                "data": base64.b64encode(json.dumps(payload).encode()).decode(),
            }
        ]
    }
    path = os.path.join(output_dir, capture_id + ".jsonl")
    with open(path, "w") as jsonl_file:
        jsonl_file.write(json.dumps(record) + "\n")
    return path


def graph_document(sort_order):
    return {
        "nodes": [
            {
                "id": "model_1",
                "type": "model_inference",
                "parameters": {"detection_sort_order": sort_order},
            }
        ],
        "connections": [],
    }


def id_box_pairs(entries):
    """The (id, box, confidence, label) pairs of a Detection_List as a
    canonical multiset (sorted tuple list)."""
    return sorted(
        (
            entry["id"],
            entry["x_min"],
            entry["y_min"],
            entry["x_max"],
            entry["y_max"],
            entry["confidence"],
            entry["label"],
        )
        for entry in entries
    )


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------


@given(
    raw_entries=raw_detection_entries(),
    sort_order=st.sampled_from(detections.SORT_ORDERS),
)
def test_property_1_id_stability_within_a_run(raw_entries, sort_order):
    """**Feature: detection-guided-bedrock-inspection, Property 1: ID
    stability within a run**

    Every consumer of the run's Detection_List sees the same entries with
    the same Detection_IDs — the list is built once and cached on the run
    state. Building twice from the same cache yields identical
    entries/IDs, even after the capture record is gone.

    **Validates: Requirements 1.3, 1.10, 2.7**
    """
    document = graph_document(sort_order)
    with tempfile.TemporaryDirectory() as output_dir:
        record_path = write_capture_record(output_dir, "run", raw_entries)
        cache = {}
        first_consumer = {}
        detections.merge_detections(
            first_consumer, output_dir, "run", document, cache
        )
        # The record disappearing between consumers proves the second
        # build comes from the run-state cache, not a re-read.
        os.remove(record_path)
        second_consumer = {}
        detections.merge_detections(
            second_consumer, output_dir, "run", document, cache
        )

    first = first_consumer["detections"]
    second = second_consumer["detections"]
    assert second == first
    assert [entry["id"] for entry in second] == [entry["id"] for entry in first]
    assert first_consumer["detection_count"] == second_consumer["detection_count"]
    # Detection_IDs are unique within the run (Requirement 1.3).
    ids = [entry["id"] for entry in first]
    assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------


@given(
    raw_entries=raw_detection_entries(),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
)
def test_property_2_order_id_independence_across_builds(raw_entries, seed):
    """**Feature: detection-guided-bedrock-inspection, Property 2:
    Order/ID independence**

    Detection_IDs are drawn from uuid4 (here a seeded rng standing in for
    the same draw sequence), never from list position: building the same
    raw detections under every sort order yields permutations of the same
    (id, box) pairs — an ID always stays with its box, never with its
    position.

    **Validates: Requirements 1.3, 1.4**
    """
    builds = {
        sort_order: detections.build_detection_list(
            raw_entries, sort_order, rng=random.Random(seed)
        )
        for sort_order in detections.SORT_ORDERS
    }
    reference = id_box_pairs(builds[detections.SORT_LEFT_TO_RIGHT])
    for sort_order, built in builds.items():
        assert id_box_pairs(built) == reference, sort_order


@given(raw_entries=raw_detection_entries())
def test_property_2_resorting_never_relabels_an_entry(raw_entries):
    """**Feature: detection-guided-bedrock-inspection, Property 2:
    Order/ID independence**

    Re-sorting one built Detection_List (real uuid4 IDs) under every sort
    order permutes the very same entries: the (id, box) pair multiset is
    invariant, so an ID never tracks position.

    **Validates: Requirements 1.3, 1.4**
    """
    built = detections.build_detection_list(raw_entries, detections.DEFAULT_SORT_ORDER)
    reference = id_box_pairs(built)
    for sort_order in detections.SORT_ORDERS:
        resorted = detections.sort_detection_list(built, sort_order)
        assert id_box_pairs(resorted) == reference, sort_order
        # A permutation of the same entry objects — nothing re-labeled,
        # nothing copied, nothing dropped.
        assert len(resorted) == len(built)
        for entry in resorted:
            assert any(entry is original for original in built)
