#  Copyright  Amazon Web Services, Inc.
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
"""Property-based tests for the shared Class_Label_Map resolver.

Covers the object-detection-visualization design's Property 5 (class-label
resolution falls back to the index string). Pure Python, no triton/torch needed.
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from lyra_science_processing_utils.utils.class_label_map import (
    COCO_CLASS_LABELS,
    resolve_class_label,
)


# A strategy for "index" inputs: ints and numeric strings (the documented
# supported forms), plus non-numeric strings to exercise the never-raises
# guarantee for indices the map cannot resolve.
_int_indices = st.integers(min_value=-1000, max_value=1000)
_numeric_string_indices = _int_indices.map(str)
_non_numeric_strings = st.text(max_size=8).filter(
    lambda s: not s.strip().lstrip("+-").isdigit()
)
_index_strategy = st.one_of(
    _int_indices, _numeric_string_indices, _non_numeric_strings
)

# A strategy for arbitrary class maps: keys may be ints or strings, values are
# arbitrary label text. Includes the empty map and None (meaning "use COCO").
_class_map_strategy = st.one_of(
    st.none(),
    st.dictionaries(
        keys=st.one_of(_int_indices, _int_indices.map(str)),
        values=st.text(max_size=12),
        max_size=10,
    ),
)


def _expected_label(class_index, class_map):
    """Reference oracle mirroring the resolver's contract: mapped label when the
    resolved map contains the (raw or int-coerced) index, else str(index)."""
    resolved_map = class_map if class_map is not None else COCO_CLASS_LABELS

    # Raw-key lookup first (supports maps keyed by the original type).
    try:
        if class_index in resolved_map:
            return str(resolved_map[class_index])
    except TypeError:
        return str(class_index)

    # Numeric coercion, then int-key and str-key lookup.
    int_index = None
    if isinstance(class_index, int):
        int_index = class_index
    elif isinstance(class_index, str):
        try:
            int_index = int(class_index.strip())
        except (ValueError, TypeError):
            int_index = None

    if int_index is not None:
        if int_index in resolved_map:
            return str(resolved_map[int_index])
        if str(int_index) in resolved_map:
            return str(resolved_map[str(int_index)])

    return str(class_index)


# Feature: object-detection-visualization, Property 5: Class-label resolution falls back to the index string
# Validates: Requirements 3.2, 3.3
@settings(max_examples=25)
@given(class_index=_index_strategy, class_map=_class_map_strategy)
def test_resolve_class_label_maps_when_present_else_index_string(
    class_index, class_map
):
    """For any index and any map, the resolved label equals the mapped name when
    the map contains the index, and otherwise equals str(index). The resolver
    must never raise for non-numeric or missing indices."""
    result = resolve_class_label(class_index, class_map)

    # Never raises and always yields a string.
    assert isinstance(result, str)

    # Matches the mapped-when-present / index-string-otherwise contract.
    assert result == _expected_label(class_index, class_map)


# Feature: object-detection-visualization, Property 5: Class-label resolution falls back to the index string
# Validates: Requirements 3.2, 3.3
@settings(max_examples=25)
@given(
    class_index=st.integers(min_value=0, max_value=79),
    label=st.text(min_size=1, max_size=12),
)
def test_resolve_class_label_returns_mapped_name_when_index_present(
    class_index, label
):
    """When the provided map contains an entry for the index (int or numeric
    string), the mapped label is returned rather than the index string."""
    int_result = resolve_class_label(class_index, {class_index: label})
    assert int_result == label

    str_result = resolve_class_label(str(class_index), {class_index: label})
    assert str_result == label


# Feature: object-detection-visualization, Property 5: Class-label resolution falls back to the index string
# Validates: Requirements 3.2, 3.3
@settings(max_examples=25)
@given(
    class_index=st.one_of(_index_strategy),
    class_map=st.dictionaries(
        keys=st.integers(min_value=0, max_value=79),
        values=st.text(max_size=12),
        max_size=5,
    ),
)
def test_resolve_class_label_falls_back_to_index_string_when_absent(
    class_index, class_map
):
    """When neither the raw index nor its int-coerced form is a key in the map,
    the resolver falls back to the index rendered as a string."""
    # Reduce the input space to the "absent" case for this property.
    raw_present = False
    try:
        raw_present = class_index in class_map
    except TypeError:
        raw_present = False

    int_index = None
    if isinstance(class_index, int):
        int_index = class_index
    elif isinstance(class_index, str):
        try:
            int_index = int(class_index.strip())
        except (ValueError, TypeError):
            int_index = None
    int_present = int_index is not None and (
        int_index in class_map or str(int_index) in class_map
    )

    if raw_present or int_present:
        return  # not the case under test

    assert resolve_class_label(class_index, class_map) == str(class_index)
