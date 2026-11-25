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
"""Unit tests for the device Canonical_RLE codec
``lyra_science_processing_utils.utils.mask_rle`` (rfdetr-seg-support task 9.1;
DD5; Requirements 9.5, 10.2, 17.5).

The Portal's ``dda_manifest.rle_encode`` is the oracle for the wire format but
is a Lambda-layer module the device cannot import, so byte identity is pinned
through the shared vector file ``test/fixtures/canonical_rle_vectors.json``
(written by that encoder; re-derived by
``edge-cv-portal/backend/tests/test_dda_manifest_instance.py``): every
vector's string must come out of ``encode`` unchanged and ``decode`` must give
the vector's mask back. A pure-Python mirror of the oracle's per-pixel loop
backs a property over random masks, and ``decode(encode(m)) == m`` holds for
every mask. Pure numpy, no triton/torch needed.
"""
import json
import os
import re

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lyra_science_processing_utils.utils import mask_rle

_HERE = os.path.dirname(os.path.abspath(__file__))
VECTORS_PATH = os.path.join(_HERE, "..", "fixtures", "canonical_rle_vectors.json")


def _load_vectors():
    with open(VECTORS_PATH) as fh:
        return json.load(fh)["vectors"]


def _vector_mask(vector):
    rows = vector["mask_rows"]
    assert len(rows) == vector["height"]
    assert all(len(row) == vector["width"] for row in rows)
    return np.array([[ch == "1" for ch in row] for row in rows], dtype=bool)


def _reference_encode(mask):
    """The oracle's algorithm (``dda_manifest.rle_encode``): a per-pixel walk
    of the column-major flattening, alternating runs, background first."""
    height, width = mask.shape
    counts = []
    current = 0
    run = 0
    for p in range(width * height):
        x, y = divmod(p, height)
        value = 1 if mask[y, x] else 0
        if value == current:
            run += 1
        else:
            counts.append(run)
            current = 1 - current
            run = 1
    counts.append(run)
    return " ".join(str(c) for c in counts)


# ---------------------------------------------------------------------------
# Byte identity against the shared vectors (DD5, Req 10.2)
# ---------------------------------------------------------------------------

def test_vector_file_covers_the_named_mask_classes():
    names = {v["name"] for v in _load_vectors()}
    assert {"border_touching", "hollow", "empty", "full", "single_pixel"} <= names


@pytest.mark.parametrize("vector", _load_vectors(), ids=lambda v: v["name"])
def test_encode_is_byte_identical_to_the_portal_encoder(vector):
    mask = _vector_mask(vector)
    assert mask_rle.encode(mask) == vector["rle"]
    # The same string from a uint8 0/1 image and from a 0/255 image.
    assert mask_rle.encode(mask.astype(np.uint8)) == vector["rle"]
    assert mask_rle.encode(mask.astype(np.uint8) * 255) == vector["rle"]


@pytest.mark.parametrize("vector", _load_vectors(), ids=lambda v: v["name"])
def test_decode_reproduces_the_vector_mask(vector):
    decoded = mask_rle.decode(vector["rle"], vector["width"], vector["height"])
    assert decoded.dtype == np.bool_
    assert decoded.shape == (vector["height"], vector["width"])
    assert np.array_equal(decoded, _vector_mask(vector))


@pytest.mark.parametrize("vector", _load_vectors(), ids=lambda v: v["name"])
def test_vector_round_trips(vector):
    mask = _vector_mask(vector)
    assert np.array_equal(
        mask_rle.decode(mask_rle.encode(mask), vector["width"], vector["height"]), mask)


# ---------------------------------------------------------------------------
# Format properties
# ---------------------------------------------------------------------------

def test_counts_sum_to_width_times_height_and_start_with_background():
    mask = np.zeros((4, 6), dtype=bool)
    mask[0, 0] = True  # first pixel foreground -> leading 0 run
    mask[2:4, 3:5] = True
    rle = mask_rle.encode(mask)
    counts = [int(c) for c in rle.split()]
    assert counts[0] == 0
    assert sum(counts) == 24
    assert sum(counts[1::2]) == int(mask.sum())  # foreground runs = mask area


def test_all_background_is_a_single_run():
    assert mask_rle.encode(np.zeros((3, 5), dtype=bool)) == "15"
    assert not mask_rle.decode("15", 5, 3).any()


def test_all_foreground_is_a_zero_then_one_run():
    assert mask_rle.encode(np.ones((3, 5), dtype=bool)) == "0 15"
    assert mask_rle.decode("0 15", 5, 3).all()


def test_order_is_column_major():
    """A single foreground pixel at (x=2, y=1) in a 4x3 mask sits at
    column-major index 2 * 3 + 1 = 7."""
    mask = np.zeros((3, 4), dtype=bool)
    mask[1, 2] = True
    assert mask_rle.encode(mask) == "7 1 4"


def test_encode_rejects_non_2d_and_empty_masks():
    with pytest.raises(ValueError, match="2-D"):
        mask_rle.encode(np.zeros((6,), dtype=bool))
    with pytest.raises(ValueError, match="2-D"):
        mask_rle.encode(np.zeros((2, 3, 1), dtype=bool))
    with pytest.raises(ValueError, match="positive"):
        mask_rle.encode(np.zeros((0, 4), dtype=bool))


def test_decode_applies_the_portal_decoder_rules():
    with pytest.raises(ValueError, match="invalid RLE counts"):
        mask_rle.decode("1 a", 2, 2)
    with pytest.raises(ValueError, match="non-negative"):
        mask_rle.decode("5 -1", 2, 2)
    with pytest.raises(ValueError, match=r"sum 3 != width\*height 4"):
        mask_rle.decode("1 2", 2, 2)
    with pytest.raises(ValueError, match=r"sum 0 != width\*height 4"):
        mask_rle.decode("", 2, 2)
    with pytest.raises(ValueError, match="positive"):
        mask_rle.decode("4", 0, 4)


def test_module_exposes_exactly_encode_and_decode_and_imports_no_portal_layer():
    assert mask_rle.__all__ == ["encode", "decode"]
    assert sorted(n for n in vars(mask_rle)
                  if callable(getattr(mask_rle, n)) and not n.startswith("_")) == [
        "decode", "encode"]
    with open(mask_rle.__file__) as fh:
        source = fh.read()
    # The Portal layer is not in the LocalServer image (DD5).
    assert re.search(r"^\s*(from|import)\s+dda_manifest", source, re.M) is None


# ---------------------------------------------------------------------------
# Properties over random masks
# ---------------------------------------------------------------------------

@settings(max_examples=200, deadline=None)
@given(
    width=st.integers(min_value=1, max_value=12),
    height=st.integers(min_value=1, max_value=12),
    data=st.data(),
)
def test_property_encode_matches_the_oracle_algorithm_and_round_trips(width, height, data):
    flat = data.draw(st.lists(st.booleans(), min_size=width * height,
                              max_size=width * height))
    mask = np.array(flat, dtype=bool).reshape(height, width)

    rle = mask_rle.encode(mask)

    assert rle == _reference_encode(mask)
    assert sum(int(c) for c in rle.split()) == width * height
    assert np.array_equal(mask_rle.decode(rle, width, height), mask)


def test_large_mask_round_trips():
    """A 1080p-sized mask with several components, the shape the decoder
    hands the marshal (DD3 / DD5)."""
    mask = np.zeros((1080, 1920), dtype=bool)
    mask[100:600, 200:900] = True
    mask[700:710, 5:1900:3] = True
    mask[-1, -1] = True
    rle = mask_rle.encode(mask)
    assert sum(int(c) for c in rle.split()) == 1080 * 1920
    assert np.array_equal(mask_rle.decode(rle, 1920, 1080), mask)
