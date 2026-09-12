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
"""Pure unit tests for the device Image_Source ROI -> videocrop splice.

Covers ``workflow_engine.roi_crop``: validation of the persisted
``imageCrop`` shape, the no-op and does-not-fit guards, the
already-crops detection that stops a graph being double-cropped, and the
placement rule that puts ``videocrop`` after the source node's LAST
element (so it lands downstream of the injected ``bayer2rgb`` and the
compiled ``videoconvert``, both of which carry the same nodeId).
"""
import pytest

from workflow_engine import roi_crop


# --------------------------------------------------------------------------
# normalized_crop
# --------------------------------------------------------------------------

class TestNormalizedCrop:
    def test_accepts_the_persisted_shape(self):
        """The real device value: four int pixel edge insets."""
        crop = roi_crop.normalized_crop(
            {"top": 1145, "bottom": 767, "left": 1369, "right": 679}
        )
        assert crop == {"top": 1145, "bottom": 767, "left": 1369, "right": 679}

    def test_missing_edges_default_to_zero(self):
        """Matching the catalog crop node's and videocrop's own defaults."""
        assert roi_crop.normalized_crop({"top": 10}) == {
            "top": 10, "bottom": 0, "left": 0, "right": 0
        }

    def test_all_zero_is_valid_but_a_no_op(self):
        crop = roi_crop.normalized_crop(
            {"top": 0, "bottom": 0, "left": 0, "right": 0}
        )
        assert crop == {"top": 0, "bottom": 0, "left": 0, "right": 0}
        assert roi_crop.is_no_op(crop) is True

    def test_integral_floats_and_digit_strings_are_accepted(self):
        """A JSON round-trip can widen an int to a float, and older
        records can carry strings; both still describe whole pixels."""
        assert roi_crop.normalized_crop(
            {"top": 10.0, "bottom": "20", "left": 0, "right": 0}
        ) == {"top": 10, "bottom": 20, "left": 0, "right": 0}

    @pytest.mark.parametrize("value", [
        None,
        "not a mapping",
        42,
        [],
        {},                                   # no edge at all
        {"top": -1},                          # negative inset
        {"top": 10.5},                        # fractional pixel
        {"top": "abc"},                       # unparsable
        {"top": True},                        # bool is not a pixel count
        {"top": None, "bottom": None,
         "left": None, "right": None},        # every edge absent
    ])
    def test_unusable_values_yield_none(self, value):
        """None means 'do not touch the document' — the caller reports it
        and runs the workflow uncropped rather than failing it."""
        assert roi_crop.normalized_crop(value) is None


# --------------------------------------------------------------------------
# fits_frame / cropped_size
# --------------------------------------------------------------------------

class TestFitsFrame:
    def test_the_device_roi_fits_the_device_frame(self):
        crop = {"top": 1145, "bottom": 767, "left": 1369, "right": 679}
        assert roi_crop.fits_frame(crop, 4608, 3288) is True
        assert roi_crop.cropped_size(crop, 4608, 3288) == (2560, 1376)

    @pytest.mark.parametrize("crop", [
        {"top": 0, "bottom": 0, "left": 100, "right": 100},   # exactly consumed
        {"top": 0, "bottom": 0, "left": 150, "right": 100},   # over-consumed
        {"top": 50, "bottom": 50, "left": 0, "right": 0},     # height consumed
    ])
    def test_a_crop_that_consumes_a_dimension_does_not_fit(self, crop):
        """videocrop cannot negotiate a zero/negative sized frame, which
        would take the whole run down."""
        assert roi_crop.fits_frame(crop, 200, 100) is False

    @pytest.mark.parametrize("width,height", [
        (0, 100), (100, 0), (None, 100), (100, None), ("wide", 100),
    ])
    def test_unknown_dimensions_cannot_be_confirmed(self, width, height):
        crop = {"top": 1, "bottom": 1, "left": 1, "right": 1}
        assert roi_crop.fits_frame(crop, width, height) is False


# --------------------------------------------------------------------------
# has_explicit_crop
# --------------------------------------------------------------------------

def make_document(elements, extra_segments=()):
    return {
        "segments": [{"name": "s0", "elements": list(elements)}]
        + list(extra_segments)
    }


class TestHasExplicitCrop:
    def test_false_for_a_graph_without_a_crop_node(self):
        document = make_document([
            {"nodeId": "n1", "factory": "appsrc", "args": {}},
            {"nodeId": "n1", "factory": "videoconvert", "args": {}},
        ])
        assert roi_crop.has_explicit_crop(document) is False

    def test_true_when_the_graph_already_renders_videocrop(self):
        """The author placed a Crop node, so the device ROI must defer."""
        document = make_document([
            {"nodeId": "n1", "factory": "appsrc", "args": {}},
            {"nodeId": "c1", "factory": "videocrop",
             "args": {"top": 5, "bottom": 5, "left": 5, "right": 5}},
        ])
        assert roi_crop.has_explicit_crop(document) is True

    def test_finds_a_crop_in_any_segment(self):
        document = make_document(
            [{"nodeId": "n1", "factory": "appsrc", "args": {}}],
            extra_segments=[{
                "name": "s1", "from": "t0",
                "elements": [{"nodeId": "c1", "factory": "videocrop",
                              "args": {}}],
            }],
        )
        assert roi_crop.has_explicit_crop(document) is True

    @pytest.mark.parametrize("document", [
        {}, {"segments": []}, {"segments": [{"name": "s0"}]},
        {"segments": [{"name": "s0", "elements": None}]},
    ])
    def test_tolerates_documents_without_elements(self, document):
        assert roi_crop.has_explicit_crop(document) is False


# --------------------------------------------------------------------------
# insert_crop_after_node
# --------------------------------------------------------------------------

CROP = {"top": 1145, "bottom": 767, "left": 1369, "right": 679}


class TestInsertCropAfterNode:
    def test_inserts_after_the_nodes_last_element(self):
        """videocrop cannot consume video/x-bayer, so it must land after
        the whole source chain — the injected bayer2rgb AND the compiled
        videoconvert, which share the source node's id."""
        document = make_document([
            {"nodeId": "n1", "factory": "appsrc",
             "args": {"name": "appsrc",
                      "caps": "video/x-bayer,format=bggr"}},
            {"nodeId": "n1", "factory": "bayer2rgb", "args": {}},
            {"nodeId": "n1", "factory": "videoconvert", "args": {}},
            {"nodeId": "m1", "factory": "emltriton", "args": {}},
            {"nodeId": None, "factory": "fakesink", "args": {}},
        ])

        assert roi_crop.insert_crop_after_node(document, "n1", CROP) is True

        factories = [e["factory"] for e in document["segments"][0]["elements"]]
        assert factories == [
            "appsrc", "bayer2rgb", "videoconvert", "videocrop",
            "emltriton", "fakesink",
        ]

    def test_inserted_element_carries_the_source_nodes_id_and_int_args(self):
        """The nodeId makes a videocrop negotiation failure attributable
        to the source node, like the injected bayer2rgb."""
        document = make_document([
            {"nodeId": "n1", "factory": "appsrc", "args": {}},
            {"nodeId": None, "factory": "fakesink", "args": {}},
        ])

        roi_crop.insert_crop_after_node(document, "n1", CROP)

        inserted = document["segments"][0]["elements"][1]
        assert inserted["nodeId"] == "n1"
        assert inserted["factory"] == "videocrop"
        assert inserted["args"] == CROP
        assert all(isinstance(v, int) for v in inserted["args"].values())

    def test_inserts_into_the_segment_holding_the_node(self):
        document = make_document(
            [{"nodeId": "other", "factory": "videotestsrc", "args": {}}],
            extra_segments=[{
                "name": "s1",
                "elements": [
                    {"nodeId": "n1", "factory": "appsrc", "args": {}},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }],
        )

        assert roi_crop.insert_crop_after_node(document, "n1", CROP) is True

        assert [e["factory"] for e in document["segments"][0]["elements"]] == [
            "videotestsrc"
        ]
        assert [e["factory"] for e in document["segments"][1]["elements"]] == [
            "appsrc", "videocrop", "fakesink"
        ]

    def test_returns_false_when_the_node_renders_no_element(self):
        document = make_document([
            {"nodeId": "other", "factory": "appsrc", "args": {}},
        ])
        assert roi_crop.insert_crop_after_node(document, "n1", CROP) is False
        assert len(document["segments"][0]["elements"]) == 1
