#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""rfdetr-seg-support task 10.9 (Requirements 10.8, 17.5): the device modules
groups 9-10 touch exist twice -- ``src/backend`` (canonical) and the Portal's
test sandbox ``edge-cv-portal/test-sandbox/dda_triton_resources/`` -- and both
copies must carry the Seg work.

The ``lyra_science_processing_utils`` copies are byte-identical, so the files
groups 9-10 edited are compared byte for byte. The three Triton templates
(``lfv_model_template.py``, ``marshal_for_capture_template.py``,
``inference_runtimes.py``) had already drifted before this spec and were not
reconciled, so for them this asserts the presence of the Seg hunks in both
copies through their anchor identifiers; ``inference_runtimes.py`` receives no
Seg hunk and is listed as "no change expected" (neither copy carries an
anchor).
"""
import hashlib
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_SRC = os.path.join(_ROOT, "src", "backend")
_SANDBOX = os.path.join(_ROOT, "edge-cv-portal", "test-sandbox", "dda_triton_resources")

#: lyra_science_processing_utils files groups 9-10 touch (canonical vs sandbox).
BYTE_IDENTICAL = (
    "lyra_science_processing_utils/model_processors/rf_detr_seg_postprocessor.py",
    "lyra_science_processing_utils/model_processors/rf_detr_semantic_segmentation_postprocessor.py",
    "lyra_science_processing_utils/model_processors/rf_detr_detection_postprocessor.py",
    "lyra_science_processing_utils/model_processors/__init__.py",
    "lyra_science_processing_utils/__init__.py",
    "lyra_science_processing_utils/utils/object_detection_result.py",
    "lyra_science_processing_utils/utils/mask_rle.py",
)

#: The Seg hunks of the two drifted templates, by anchor identifier.
TEMPLATE_ANCHORS = {
    "lfv_model_template.py": (
        "DETECTION_LIST_MAX_BYTES",
        "mask_truncated",
        "paint_composite(",
        "InstanceSegmentationResult",
    ),
    "marshal_for_capture_template.py": (
        "_has_instance_masks",
        "_instance_color_map",
        "_fill_instance_masks",
        "mask_color_map",
        "detections_truncated",
    ),
}

#: No Seg hunk is expected in this template; neither copy may carry one.
NO_CHANGE_EXPECTED = ("inference_runtimes.py",)
_ALL_ANCHORS = tuple(sorted({a for anchors in TEMPLATE_ANCHORS.values() for a in anchors}))


def _template_paths(name):
    return (
        os.path.join(_SRC, "dda_triton", "resources_for_copy", name),
        os.path.join(_SANDBOX, name),
    )


def _sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


@pytest.mark.parametrize("relative", BYTE_IDENTICAL)
def test_lyra_science_copy_is_byte_identical(relative):
    canonical = os.path.join(_SRC, relative)
    sandbox = os.path.join(_SANDBOX, relative)
    assert os.path.isfile(canonical), canonical
    assert os.path.isfile(sandbox), sandbox
    assert _sha256(canonical) == _sha256(sandbox), (
        "Requirement 17.5: {} differs between src/backend and the test sandbox; "
        "copy the changed file (src/backend is canonical)".format(relative)
    )


@pytest.mark.parametrize("name,anchors", sorted(TEMPLATE_ANCHORS.items()))
def test_template_seg_hunks_present_in_both_copies(name, anchors):
    for path in _template_paths(name):
        assert os.path.isfile(path), path
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        missing = [anchor for anchor in anchors if anchor not in text]
        assert missing == [], (
            "Requirement 10.8: {} lacks the Seg hunk(s) {} in {}".format(name, missing, path)
        )


@pytest.mark.parametrize("name", NO_CHANGE_EXPECTED)
def test_inference_runtimes_carries_no_seg_hunk(name):
    for path in _template_paths(name):
        assert os.path.isfile(path), path
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        present = [anchor for anchor in _ALL_ANCHORS if anchor in text]
        assert present == [], "{} was not expected to change for Seg; found {}".format(path, present)


def test_template_seg_hunks_have_the_same_anchor_counts():
    """The two copies received the same hunks: every anchor occurs the same
    number of times in both (drifted context aside)."""
    for name, anchors in TEMPLATE_ANCHORS.items():
        canonical, sandbox = _template_paths(name)
        with open(canonical, "r", encoding="utf-8") as handle:
            left = handle.read()
        with open(sandbox, "r", encoding="utf-8") as handle:
            right = handle.read()
        for anchor in anchors:
            assert left.count(anchor) == right.count(anchor), (
                "{}: anchor {!r} occurs {} times in src/backend and {} times in the sandbox"
                .format(name, anchor, left.count(anchor), right.count(anchor))
            )
