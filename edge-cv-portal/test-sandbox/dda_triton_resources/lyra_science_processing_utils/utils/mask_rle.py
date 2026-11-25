#  #
#   Copyright  Amazon Web Services, Inc.
#  #
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#  #
#        http://www.apache.org/licenses/LICENSE-2.0
#  #
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#  #
"""Canonical_RLE codec for instance masks on the device (rfdetr-seg-support
DD5; Requirements 9.5, 10.2).

The wire format is the Portal's ``dda_manifest.rle_encode`` string: the run
lengths of a ``height x width`` binary mask flattened in **column-major**
order, space-separated, the first run counting background pixels (so a mask
whose first pixel is foreground starts with ``0``), summing to
``width * height``. COCO's uncompressed RLE uses the same column-major order,
so the counts can be copied into a ``{"counts": [...], "size": [h, w]}``
dict unchanged.

``rle_encode`` is a pure-Python loop over every pixel -- too slow for a 2 MP
mask on a Jetson -- so this module implements the identical encoding with
numpy. It is pinned byte-for-byte against the strings the Portal encoder
produces through the shared vector file ``test/fixtures/canonical_rle_vectors
.json`` (``test/backend-test/test_mask_rle.py``). It imports nothing from
``dda_manifest``: that module is a Portal Lambda layer, not part of the
LocalServer image.

Exactly two functions are exposed: ``encode(mask) -> str`` and
``decode(rle, width, height) -> np.ndarray[bool]``.
"""
import numpy as np

__all__ = ["encode", "decode"]


def encode(mask) -> str:
    """Encode a 2-D binary mask (rows x columns, row-major as an image is
    stored) into Canonical_RLE counts.

    Any array-like of shape ``(height, width)`` is accepted; non-zero values
    are foreground. Returns the space-separated run lengths over the
    column-major flattening, background run first.

    Raises:
        ValueError: the mask is not two-dimensional or has no pixels
    """
    arr = np.asarray(mask)
    if arr.ndim != 2:
        raise ValueError(f"mask must be 2-D (height, width), got shape {arr.shape}")
    if arr.size == 0:
        raise ValueError("width and height must be positive")
    flat = np.ascontiguousarray(arr.T).reshape(-1).astype(bool, copy=False)
    # Indices where the value changes from the previous pixel: each is the
    # start of a new run, so the run lengths are the gaps between them.
    changes = np.flatnonzero(flat[1:] != flat[:-1]) + 1
    counts = np.diff(np.concatenate(([0], changes, [flat.size])))
    if flat[0]:
        # The first run is foreground: the background run before it is empty.
        counts = np.concatenate(([0], counts))
    return " ".join(str(int(c)) for c in counts)


def decode(rle: str, width: int, height: int) -> np.ndarray:
    """Decode Canonical_RLE counts into a ``(height, width)`` boolean mask.

    Applies the Portal decoder's rules: every token an integer, no negative
    count, and the counts summing to ``width * height``.

    Raises:
        ValueError: width or height not positive, a non-integer token, a
            negative count, or counts not summing to width*height
    """
    if width < 1 or height < 1:
        raise ValueError("width and height must be positive")
    try:
        counts = np.array([int(token) for token in rle.split()], dtype=np.int64)
    except (AttributeError, ValueError):
        raise ValueError(f"invalid RLE counts: {rle!r}")
    if counts.size and counts.min() < 0:
        raise ValueError("RLE counts must be non-negative")
    total = int(counts.sum())
    if total != width * height:
        raise ValueError(f"RLE counts sum {total} != width*height {width * height}")
    # Runs alternate background, foreground, background, ...: odd-indexed
    # runs are foreground. Expand them over the column-major flattening and
    # transpose back to the image's (height, width) layout.
    values = (np.arange(counts.size) % 2) == 1
    flat = np.repeat(values, counts)
    return np.ascontiguousarray(flat.reshape(width, height).T)
