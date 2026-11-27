# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
The Grayscale_Node handler of car-assembly-check (task 13.2; design §8,
DD11; Requirements 6.1-6.3).

`samples/car-assembly-check/grayscale_handler.py` is the exact text pasted
into a `custom_python_preprocess` node's `code`. The bridge runs it with
`cv2` and `np` pre-imported, so the test executes the same file with those
two names injected into its globals:

* synthetic cases -- R = G = B for `RGB` and `BGR`, `GRAY8` returns `None`
  (pass-through), any other format (`RGBA` included) raises naming it, and
  the output keeps the input's shape and dtype;
* the parity case -- every colour / grayscale pair with the same filename
  under `/home/ubuntu/data/assembly-photos/{original,monochrome}/` is
  discovered (at least one `anomaly_*` pair when present); three colour
  originals, one of them `anomaly_*`, are decoded with Pillow
  (`ImageOps.exif_transpose` first), run through the handler and compared
  with the grayscale JPEG decoded as RGB: `|diff| <= 2` for >= 99.9 % of the
  pixels.

Both need OpenCV and skip with that reason without it; the parity case also
skips when the local image pairs are absent.
"""
import os

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip(
    "cv2", reason="OpenCV (cv2) is not installed: the handler runs with cv2 injected "
                  "(run in the python3 lane)")

_HERE = os.path.dirname(os.path.abspath(__file__))
HANDLER = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "samples", "car-assembly-check",
                                       "grayscale_handler.py"))
PHOTOS = "/home/ubuntu/data/assembly-photos"
ORIGINAL = os.path.join(PHOTOS, "original")
MONOCHROME = os.path.join(PHOTOS, "monochrome")


def load_handler():
    """The handler file executed as a module with cv2 and np injected, as
    the bridge does."""
    with open(HANDLER, "r", encoding="utf-8") as handle:
        source = handle.read()
    namespace = {"cv2": cv2, "np": np, "__name__": "grayscale_handler"}
    exec(compile(source, HANDLER, "exec"), namespace)
    return namespace["process_frame"]


def frame_of(fmt, shape=(6, 8, 3)):
    rng = np.random.default_rng(7)
    return rng.integers(0, 256, size=shape, dtype=np.uint8), {"frame": {"format": fmt}}


@pytest.mark.parametrize("fmt", ["RGB", "BGR"])
def test_colour_formats_become_three_equal_channels(fmt):
    process_frame = load_handler()
    frame, metadata = frame_of(fmt)
    out = process_frame(frame, metadata)
    assert out.shape == frame.shape and out.dtype == frame.dtype
    assert np.array_equal(out[..., 0], out[..., 1]) and np.array_equal(out[..., 1], out[..., 2])
    weights = (0.299, 0.587, 0.114) if fmt == "RGB" else (0.114, 0.587, 0.299)
    expected = np.rint(frame.astype(float) @ np.array(weights))
    assert np.max(np.abs(out[..., 0].astype(float) - expected)) <= 1


def test_gray8_passes_through():
    process_frame = load_handler()
    frame, metadata = frame_of("GRAY8", shape=(6, 8))
    assert process_frame(frame, metadata) is None


@pytest.mark.parametrize("metadata", [{"frame": {"format": "RGBA"}}, {"frame": {}}, {}])
def test_other_formats_raise_naming_the_format(metadata):
    process_frame = load_handler()
    frame, _ = frame_of("RGBA", shape=(6, 8, 4))
    fmt = (metadata.get("frame") or {}).get("format")
    with pytest.raises(ValueError, match="unsupported frame format %r" % (fmt,)):
        process_frame(frame, metadata)


def _pairs():
    if not (os.path.isdir(ORIGINAL) and os.path.isdir(MONOCHROME)):
        return []
    return sorted(set(os.listdir(ORIGINAL)) & set(os.listdir(MONOCHROME)))


@pytest.mark.skipif(not _pairs(), reason="the local assembly-photos colour/grayscale pairs are absent")
def test_parity_with_the_training_grayscale():
    from PIL import Image, ImageOps

    pairs = _pairs()
    anomaly = [name for name in pairs if name.startswith("anomaly_")]
    if any(name.startswith("anomaly_") for name in os.listdir(ORIGINAL)):
        assert anomaly, "an anomaly_* original has no grayscale twin"
    regular = [name for name in pairs if not name.startswith("anomaly_")]
    chosen = regular[:2] + anomaly[:1] if anomaly else regular[:3]
    assert len(chosen) >= 3
    print("pairs discovered: {0} ({1} anomaly_*); checked: {2}".format(
        len(pairs), len(anomaly), ", ".join(chosen)))

    process_frame = load_handler()
    for name in chosen:
        with Image.open(os.path.join(ORIGINAL, name)) as image:
            colour = np.asarray(ImageOps.exif_transpose(image).convert("RGB"))
        with Image.open(os.path.join(MONOCHROME, name)) as image:
            reference = np.asarray(ImageOps.exif_transpose(image).convert("RGB"))
        out = process_frame(np.ascontiguousarray(colour), {"frame": {"format": "RGB"}})
        assert out.shape == reference.shape, name
        close = np.abs(out.astype(np.int16) - reference.astype(np.int16)) <= 2
        fraction = float(close.mean())
        print("{0}: {1:.5%} of pixels within 2".format(name, fraction))
        assert fraction >= 0.999, name
