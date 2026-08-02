import numpy
import typing


# Default palette used for anomaly masks.
DEFAULT_ANOMALY_MASK_PALETTE = numpy.array(
    [
        [255, 255, 255],
        [35, 164, 54],
        [20, 163, 179],
        [247, 105, 2],
        [147, 128, 255],
        [189, 16, 224],
        [70, 124, 230],
        [160, 147, 8],
        [0, 148, 189],
        [204, 119, 0],
        [88, 86, 214],
        [214, 28, 176],
        [0, 122, 255],
        [138, 110, 0],
        [44, 160, 44],
        [49, 130, 189],
        [230, 85, 13],
        [130, 105, 255],
        [255, 46, 220],
        [37, 50, 234],
        [124, 159, 30],
        [84, 166, 17],
        [24, 165, 153],
        [246, 103, 60],
        [163, 88, 255],
        [255, 67, 103],
        [109, 128, 252],
        [152, 152, 27],
        [65, 117, 5],
        [0, 152, 194],
        [173, 73, 74],
        [108, 115, 255],
        [252, 59, 220],
        [67, 59, 247],
        [159, 150, 30],
        [44, 170, 44],
        [24, 160, 175],
        [230, 115, 0],
        [132, 116, 212],
        [222, 115, 122],
        [148, 103, 189],
        [99, 121, 57],
        [149, 149, 9],
        [44, 147, 165],
        [132, 60, 57],
        [50, 48, 197],
        [222, 94, 184],
        [82, 84, 163],
        [117, 136, 68],
        [49, 163, 84],
        [20, 149, 243],
        [231, 111, 113],
        [38, 36, 148],
        [234, 98, 157],
        [107, 110, 207],
        [123, 150, 49],
        [62, 156, 43],
        [71, 156, 205],
        [190, 78, 22],
        [93, 91, 215],
        [123, 65, 115],
        [136, 139, 215],
        [122, 141, 52],
        [70, 164, 72],
        [64, 154, 196],
        [172, 118, 79],
        [126, 64, 203],
        [165, 81, 148],
        [51, 150, 255],
        [140, 109, 49],
        [63, 149, 55],
        [31, 119, 180],
        [214, 39, 40],
        [251, 70, 206],
        [206, 109, 189],
        [117, 107, 177],
        [173, 145, 52],
        [76, 166, 58],
        [0, 160, 204],
        [235, 109, 0],
        [207, 99, 144],
        [207, 114, 193],
        [132, 127, 184],
        [180, 133, 24],
        [133, 155, 59],
        [0, 112, 143],
        [184, 132, 127],
        [255, 79, 77],
        [57, 59, 121],
        [182, 138, 43],
        [124, 126, 2],
        [0, 144, 184],
        [166, 87, 8],
        [90, 142, 214],
        [214, 97, 107],
        [142, 44, 157],
        [120, 120, 38],
        [84, 141, 22],
        [0, 109, 139],
        [235, 112, 5],
        [48, 150, 209],
        [140, 86, 75],
        [114, 24, 119],
        [133, 136, 2]
    ],
    dtype=numpy.uint8,
)


def convert_index_mask_to_color_mask(
    mask: numpy.array,
    palette: numpy.array = DEFAULT_ANOMALY_MASK_PALETTE,
) -> numpy.array:
    """
    Converts index anomaly mask to a RGB image using provided palette.

    :param mask: Class index mask of shape (width, height).
    :param palette: Palette that maps class index to RGB color.
    :return: RGB anomaly mask.
    """

    if mask.dtype != numpy.dtype(numpy.uint8):
        mask = mask.astype(numpy.uint8)
    max_value_in_mask = int(mask.max())

    replacement_colors = palette[: max_value_in_mask + 1]
    rgb_mask = replacement_colors[mask]

    return rgb_mask


def convert_color_mask_to_index_mask(
    rgb_mask: numpy.array,
    classes_colors: numpy.array,
    missing_class_index: int = -1,
) -> numpy.array:
    """
    Converts RGB anomaly mask to an index mask.

    :param rgb_mask: RGB anomaly mask.
    :param classes_colors: List of colors. Index of a color in the list is class id.
    :param missing_class_index: Value in the resulting mask to be used for colors that do not map to a class in classes_colors.
    :return: index anomaly mask.
    """

    result = numpy.full(
        (rgb_mask.shape[0], rgb_mask.shape[1]),
        missing_class_index,
        dtype=numpy.int,
    )
    for color_idx, color in enumerate(classes_colors):
        color_mask = numpy.all(rgb_mask == color, axis=-1)
        result = (1.0 - color_mask) * result + color_mask * color_idx
    return result


def get_classes_areas(
    mask: numpy.array,
) -> typing.List[typing.Tuple[int, float]]:
    """
    Computes areas occupied by classes in the index mask.

    :param mask: Class index mask of shape (width, height) with int elements.
    :return: List of tuples. The first element in the tuple is class index,
        second element - the percentage of the mask area occupied by the class.
    """

    if len(mask.shape) != 2:
        raise ValueError(f"Unexpected mask shape ${mask.shape}.")
    image_area = mask.shape[1] * mask.shape[0]
    pixels_per_class = numpy.bincount(mask.ravel())
    return [
        (class_index, pixles_count / image_area)
        for class_index, pixles_count in enumerate(pixels_per_class)
        if pixles_count > 0
    ]


def hex_color_string(
    color: typing.Iterable[int],
) -> str:
    """
    Converts RGB color to hex string representation.

    :param color: RGB color. Iterable with 3 integer elements.
    :returns: Hex string that represents color. For example #AABBCC.
    """

    if len(color) != 3:
        raise ValueError(f"Unexpected color format {color}. Color must contain 3 int elements.")
    for channel in color:
        if not isinstance(channel, int) or channel > 255 or channel < 0:
            raise ValueError(
                f"Unexpected color format {color}. Color must contain 3 int elements in range [0, 255]."
            )
    return f"#{color[0]:02X}{color[1]:02X}{color[2]:02X}"


def color_from_hex_string(
    color_string: str,
) -> typing.List[int]:
    """
    Converts color hex string to list with color components.
    """

    if len(color_string) != 7 or color_string[0] != "#":
        raise ValueError("Invalid color string format.")
    return [
        int(color_string[1:3], 16),
        int(color_string[3:5], 16),
        int(color_string[5:7], 16),
    ]
