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
#  #
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  #
#      http://www.apache.org/licenses/LICENSE-2.0
#  #
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import numpy
import math
import pytest

from lyra_anomalies_mask_utils import (
    convert_index_mask_to_color_mask,
    convert_color_mask_to_index_mask,
    get_classes_areas,
    hex_color_string,
    color_from_hex_string,
)


def test__convert_index_mask_to_color_mask__with_default_palette():
    # Arrange
    index_mask = numpy.array(
        [[0, 0], [1, 2]],
        dtype=numpy.uint8,
    )

    # Act
    rgb_mask = convert_index_mask_to_color_mask(index_mask)

    # Assert
    assert numpy.array_equal(
        rgb_mask[0, 0],
        [255, 255, 255],
    )
    assert numpy.array_equal(
        rgb_mask[1, 0],
        [35, 164, 54],
    )
    assert numpy.array_equal(
        rgb_mask[1, 1],
        [20, 163, 179],
    )


def test__convert_index_mask_to_color_mask__with_custom_palette():
    # Arrange
    index_mask = numpy.array(
        [
            [0, 0],
            [1, 2],
        ],
    )
    custom_palette = numpy.array(
        [
            [0, 0, 0],
            [1, 1, 1],
            [2, 2, 2],
        ],
        dtype=numpy.uint8,
    )

    # Act
    rgb_mask = convert_index_mask_to_color_mask(
        index_mask,
        custom_palette,
    )

    # Assert
    assert numpy.array_equal(
        rgb_mask[0, 0],
        [0, 0, 0],
    )
    assert numpy.array_equal(
        rgb_mask[1, 0],
        [1, 1, 1],
    )
    assert numpy.array_equal(
        rgb_mask[1, 1],
        [2, 2, 2],
    )


def test__convert_color_mask_to_index_mask():
    # Arrange
    rgb_mask = numpy.array(
        [
            [[0, 0, 0], [1, 2, 3], [1, 2, 3]],
            [[255, 255, 255], [0, 0, 0], [66, 66, 66]],
            [[255, 255, 255], [255, 255, 255], [0, 0, 0]],
        ],
        dtype=numpy.uint8,
    )
    classes_colors = numpy.array(
        [
            [0, 0, 0],
            [1, 2, 3],
            [255, 255, 255],
        ],
        dtype=numpy.uint8,
    )
    expected_result = numpy.array(
        [
            [0, 1, 1],
            [2, 0, -1],
            [2, 2, 0],
        ],
        dtype=numpy.uint8,
    )

    # Act
    result = convert_color_mask_to_index_mask(
        rgb_mask,
        classes_colors,
    )

    # Assert
    numpy.array_equal(
        result,
        expected_result,
    )


def test__get_classes_areas():
    # Arrange
    index_mask = numpy.array(
        [
            [0, 1, 2],
            [0, 1, 2],
            [2, 4, 5],
        ],
        dtype=numpy.uint8,
    )
    expected_classes_areas = [
        (0, 2 / 9),
        (1, 2 / 9),
        (2, 3 / 9),
        (4, 1 / 9),
        (5, 1 / 9),
    ]

    # Act
    actual_classes_areas = get_classes_areas(index_mask)

    # Assert
    assert len(actual_classes_areas) == 5
    for actual_class, expected_class in zip(actual_classes_areas, expected_classes_areas):
        assert actual_class[0] == expected_class[0]
        assert math.isclose(actual_class[1], expected_class[1])


def test__get_classes_areas__invalid_input_mask__raises():
    # Arrange
    index_mask = numpy.zeros(
        [128, 128, 3],
        dtype=numpy.uint8,
    )

    # Act, Assert
    with pytest.raises(ValueError):
        get_classes_areas(index_mask)


@pytest.mark.parametrize(
    "test_color",
    [
        # (color, expected hex string).
        ([128, 255, 41], "#80FF29"),
        ([0, 0, 0], "#000000"),
        ([255, 255, 255], "#FFFFFF"),
    ],
)
def test__hex_color_string(
    test_color,
):
    # Act
    hex_color = hex_color_string(test_color[0])

    # Assert
    assert hex_color == test_color[1]


@pytest.mark.parametrize(
    "test_color",
    [
        [128, 255],
        [128, 255, 0, 0],
        [-1, 0, 0],
        [2553, 255, 255],
    ],
)
def test__hex_color_string__invalid_color__raises(
    test_color,
):
    # Act, Assert
    with pytest.raises(ValueError):
        hex_color_string(test_color)


@pytest.mark.parametrize(
    "test_color",
    [
        # (hex string, expected color).
        ("#000000", [0, 0, 0]),
        ("#01bbcc", [1, 187, 204]),
        ("#23BBFF", [35, 187, 255]),
    ],
)
def test__color_from_hex_string(
    test_color,
):
    # Act, Assert
    assert color_from_hex_string(test_color[0]) == test_color[1]


@pytest.mark.parametrize(
    "test_color",
    [
        "#GOOOOO",
        "!01bbcc",
        "foobar",
    ],
)
def test__color_from_hex_string__invalid_color__raises(
    test_color,
):
    # Act, Assert
    with pytest.raises(ValueError):
        color_from_hex_string(test_color)
