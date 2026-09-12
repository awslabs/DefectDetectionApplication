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
"""Aspect-preserving (letterbox) model input, opt-in per model.

The default resize squashes a frame into the model's square input, distorting
every object by the frame's aspect ratio. On a DLAP-701 that cost ~5x of
detector confidence and was the difference between zero detections and
detections. `preserve_aspect` letterboxes instead.

The critical invariant is the ROUND TRIP: a box in source coordinates, mapped
forward into letterboxed network space and back by the post-processor, must
return to where it started. `bedrock_inference`'s `crop_detection_index` crops
from these coordinates, so a wrong inverse would silently feed the wrong region
to Bedrock rather than fail loudly.
"""
import numpy as np
import pytest

from lyra_science_processing_utils.model_processors.basic_preprocessor import (
    LETTERBOX_METADATA_KEY,
    BasicPreProcessor,
    letterbox_transform,
)
from lyra_science_processing_utils.model_processors.yolo_detection_postprocessor import (
    YoloDetectionPostProcessor,
)

NET = 1280


def base_config(**overrides):
    config = {
        "image_width": NET,
        "image_height": NET,
        "image_range_scale": True,
        "normalize": False,
    }
    config.update(overrides)
    return config


def frame(width, height):
    """A deterministic non-uniform image, so a squash and a letterbox of the
    same frame cannot accidentally compare equal."""
    rng = np.random.default_rng(1234)
    return rng.integers(0, 255, size=(height, width, 3), dtype=np.uint8)


# --------------------------------------------------------------------------
# letterbox_transform: pure geometry
# --------------------------------------------------------------------------

class TestLetterboxTransform:
    def test_wide_frame_pads_vertically(self):
        """4608x3288 -> ratio 1280/4608, so height uses only part of the canvas."""
        t = letterbox_transform(4608, 3288, NET, NET)
        assert t["ratio"] == pytest.approx(NET / 4608.0)
        assert t["resized_width"] == NET
        assert t["resized_height"] == pytest.approx(round(3288 * NET / 4608.0))
        assert t["pad_x"] == 0
        assert t["pad_y"] == (NET - t["resized_height"]) // 2

    def test_tall_frame_pads_horizontally(self):
        t = letterbox_transform(1000, 2000, NET, NET)
        assert t["resized_height"] == NET
        assert t["pad_y"] == 0
        assert t["pad_x"] > 0

    def test_square_frame_needs_no_padding(self):
        """A square ROI is why an operator can get the benefit with no code."""
        t = letterbox_transform(2023, 2023, NET, NET)
        assert t["pad_x"] == 0 and t["pad_y"] == 0
        assert t["resized_width"] == t["resized_height"] == NET

    def test_ratio_is_uniform_across_axes(self):
        """The defining property: one scale for both axes, unlike the squash."""
        t = letterbox_transform(2560, 1376, NET, NET)
        assert t["resized_width"] / 2560.0 == pytest.approx(t["ratio"])
        assert t["resized_height"] / 1376.0 == pytest.approx(t["ratio"], abs=1e-3)


# --------------------------------------------------------------------------
# Pre-processor: opt-in, and default untouched
# --------------------------------------------------------------------------

class TestPreProcessorOptIn:
    def test_default_squashes_and_returns_a_bare_array(self):
        """Historical behaviour must be bit-for-bit preserved for every model
        that has not opted in."""
        import cv2

        src = frame(4608, 3288)
        out = BasicPreProcessor(base_config())(src)
        assert isinstance(out, np.ndarray)
        assert out.shape == (1, 3, NET, NET)

        # Byte-identical to the plain squash resize this always did.
        expected = cv2.resize(src, (NET, NET), interpolation=cv2.INTER_AREA)
        expected = np.expand_dims(expected, 0).transpose(
            (0, 3, 1, 2)).astype(np.float32) / 255.0
        assert np.array_equal(out, expected)

    @pytest.mark.parametrize("config", [
        base_config(preserve_aspect=True),
        base_config(preprocessing={"preserve_aspect": True}),
        base_config(detection={"preserve_aspect": True}),
    ])
    def test_opt_in_is_accepted_at_each_config_nesting(self, config):
        out = BasicPreProcessor(config)(frame(4608, 3288))
        assert isinstance(out, tuple), "letterboxed models return (image, metad)"
        image, metad = out
        assert image.shape == (1, 3, NET, NET)
        assert LETTERBOX_METADATA_KEY in metad

    def test_letterboxed_output_pads_rather_than_stretches(self):
        image, metad = BasicPreProcessor(base_config(preserve_aspect=True))(
            frame(4608, 3288))
        t = metad[LETTERBOX_METADATA_KEY]
        # Rows above the pasted content are the 114 filler, scaled by /255.
        pad_row = image[0, :, 0, :]
        assert t["pad_y"] > 0
        assert np.allclose(pad_row, 114.0 / 255.0)
        # A row inside the content is not uniform filler.
        content_row = image[0, :, t["pad_y"] + t["resized_height"] // 2, :]
        assert not np.allclose(content_row, 114.0 / 255.0)

    def test_resize_to_height_path_is_unaffected_by_the_flag(self):
        """supervised_bbox stage1/stage2 use resize_to_height; they must keep
        returning a bare array even if a flag is present."""
        out = BasicPreProcessor(base_config(preserve_aspect=True))(
            frame(1000, 500), resize_to_height=224)
        assert isinstance(out, np.ndarray)

    def test_grayscale_input_is_letterboxed_and_upconverted(self):
        rng = np.random.default_rng(7)
        gray = rng.integers(0, 255, size=(600, 900), dtype=np.uint8)
        image, _ = BasicPreProcessor(base_config(preserve_aspect=True))(gray)
        assert image.shape == (1, 3, NET, NET)


# --------------------------------------------------------------------------
# Round trip: the invariant that protects the Bedrock crops
# --------------------------------------------------------------------------

#: More anchors than channels, because `_to_anchor_rows` identifies the channel
#: axis as the smaller one -- a single-anchor tensor is genuinely ambiguous and
#: the decoder rightly refuses it.
SYNTHETIC_ANCHORS = 16


def decode_one_box(box_xyxy, src_size, preprocess_metad, network_input=NET):
    """Drive the post-processor with a single synthetic detection whose box is
    already in network space, and read back the source-space coordinates.

    Anchor 0 carries the box; the rest score 0 and are dropped by the threshold.
    """
    post = YoloDetectionPostProcessor({
        "detection": {"score_threshold": 0.01, "iou_threshold": 0.45,
                      "network_input": network_input,
                      "class_names": ["blue box"]},
        "image_width": network_input,
    })
    x_min, y_min, x_max, y_max = box_xyxy
    raw = np.zeros((1, 5, SYNTHETIC_ANCHORS), dtype=np.float32)
    raw[0, 0, 0] = (x_min + x_max) / 2.0          # cx
    raw[0, 1, 0] = (y_min + y_max) / 2.0          # cy
    raw[0, 2, 0] = x_max - x_min                  # w
    raw[0, 3, 0] = y_max - y_min                  # h
    raw[0, 4, 0] = 0.9                            # class score
    kwargs = {"src_img_size": src_size}
    if preprocess_metad is not None:
        kwargs["preprocess_metad"] = preprocess_metad
    results = post([raw], **kwargs)
    assert len(results) == 1, "expected exactly one decoded detection"
    result = results[0]
    return getattr(result, "bbox", None) or result.bounding_box


@pytest.mark.parametrize("src_w,src_h", [
    (4608, 3288),   # full sensor frame, ROI cleared
    (2560, 1376),   # the 1.86:1 ROI that detected nothing
    (2023, 1623),   # the 1.25:1 ROI that found 2 of 3
    (2023, 2023),   # a square ROI
    (1000, 2000),   # tall, pads horizontally
])
def test_source_box_survives_the_letterbox_round_trip(src_w, src_h):
    """Forward-map a known source box into network space, decode it back, and
    require it to return to where it started."""
    t = letterbox_transform(src_w, src_h, NET, NET)
    # A box well inside the frame, in SOURCE coordinates.
    sx_min, sy_min = src_w * 0.30, src_h * 0.25
    sx_max, sy_max = src_w * 0.55, src_h * 0.70
    # Forward: uniform scale then centre pad.
    net_box = (sx_min * t["ratio"] + t["pad_x"], sy_min * t["ratio"] + t["pad_y"],
               sx_max * t["ratio"] + t["pad_x"], sy_max * t["ratio"] + t["pad_y"])

    got = decode_one_box(net_box, (src_w, src_h), {LETTERBOX_METADATA_KEY: t})

    assert got[0] == pytest.approx(sx_min, abs=1.0)
    assert got[1] == pytest.approx(sy_min, abs=1.0)
    assert got[2] == pytest.approx(sx_max, abs=1.0)
    assert got[3] == pytest.approx(sy_max, abs=1.0)


def test_squash_inverse_is_unchanged_without_metadata():
    """No letterbox metadata -> the original aspect-independent scaling, so
    models that did not opt in decode exactly as before."""
    got = decode_one_box((320.0, 320.0, 640.0, 640.0), (4608, 3288), None)
    assert got[0] == pytest.approx(4608 * 320.0 / NET, abs=1.0)
    assert got[1] == pytest.approx(3288 * 320.0 / NET, abs=1.0)


def test_letterbox_and_squash_inverses_actually_differ():
    """Guards against the inverse silently falling through to the squash path,
    which would put every box in the wrong place while still 'working'."""
    t = letterbox_transform(4608, 3288, NET, NET)
    net_box = (400.0, 400.0, 700.0, 700.0)
    with_lb = decode_one_box(net_box, (4608, 3288), {LETTERBOX_METADATA_KEY: t})
    without = decode_one_box(net_box, (4608, 3288), None)
    assert abs(with_lb[1] - without[1]) > 50.0


def test_boxes_are_clamped_into_the_source_frame():
    """A detection overlapping the padding must not report coordinates outside
    the frame -- crop_detection_index crops by these numbers."""
    t = letterbox_transform(4608, 3288, NET, NET)
    # Deliberately spill into the top padding band.
    got = decode_one_box((0.0, 0.0, 200.0, float(t["pad_y"]) - 1.0),
                         (4608, 3288), {LETTERBOX_METADATA_KEY: t})
    assert got[0] >= 0.0 and got[1] >= 0.0
    assert got[2] <= 4608.0 and got[3] <= 3288.0


@pytest.mark.parametrize("metad", [
    None, {}, "not a dict", {"letterbox": {}},
    {"letterbox": {"ratio": 0}}, {"letterbox": {"ratio": "wide"}},
    {"radius": 500, "band": (1000, 1288)},      # polar-transform metadata
])
def test_unusable_or_unrelated_metadata_falls_back_to_squash(metad):
    """The polar transform shares this channel; its metadata must never be
    mistaken for a letterbox."""
    got = decode_one_box((320.0, 320.0, 640.0, 640.0), (4608, 3288), metad)
    assert got[0] == pytest.approx(4608 * 320.0 / NET, abs=1.0)


def test_preprocessor_metadata_feeds_the_postprocessor_end_to_end():
    """The two halves agree without anyone hand-building the transform: run the
    real pre-processor, hand its metadata straight to the post-processor."""
    src_w, src_h = 4608, 3288
    _, metad = BasicPreProcessor(base_config(preserve_aspect=True))(
        frame(src_w, src_h))
    t = metad[LETTERBOX_METADATA_KEY]
    sx_min, sy_min, sx_max, sy_max = 1400.0, 1100.0, 2600.0, 2300.0
    net_box = (sx_min * t["ratio"] + t["pad_x"], sy_min * t["ratio"] + t["pad_y"],
               sx_max * t["ratio"] + t["pad_x"], sy_max * t["ratio"] + t["pad_y"])
    got = decode_one_box(net_box, (src_w, src_h), metad)
    assert got[0] == pytest.approx(sx_min, abs=1.5)
    assert got[1] == pytest.approx(sy_min, abs=1.5)
    assert got[2] == pytest.approx(sx_max, abs=1.5)
    assert got[3] == pytest.approx(sy_max, abs=1.5)
