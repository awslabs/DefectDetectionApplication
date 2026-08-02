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
import numpy as np
import cv2

# This file contains functions for detecting circular objects and performing polar transform so as to
# converting rotational variation into translational variation for more generalizable defect detection on circular objects (e.g. gears)
# TODO: currently parameters are hard-coded for Precision Resource Use case, will need to make them configurable

def find_circular_obj_hough(blur, dp, min_dist, param1, param2, min_radius, max_radius):
    """
    Find circular object (such as gear in Precision Resource use case) by Hough transform
    """
    detected_circles = cv2.HoughCircles(blur, cv2.HOUGH_GRADIENT, dp, min_dist,
                                        param1=param1,param2=param2,minRadius=min_radius,maxRadius=max_radius)
    if detected_circles is None:
        return None, None, None

    largest_circle = detected_circles[0][0]
    largest_circle = np.uint16(np.around(largest_circle))
    return largest_circle[0], largest_circle[1], largest_circle[2]

def find_circular_obj_contour(binary):
    """
    Find circular object (such as gear in Precision Resource use case) by contour detection
    """
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    largest_contour = max(contours, key=cv2.contourArea)

    (x, y), radius = cv2.minEnclosingCircle(largest_contour)
    largest_circle = np.uint16(np.around([x, y, radius]))
    return largest_circle[0], largest_circle[1], largest_circle[2]

def _find_non_zero(proj, thr=0):
    nonzero_indx = np.argwhere(proj>thr).squeeze()
    start, end = (nonzero_indx[0], nonzero_indx[-1])
    return start, end

def find_circular_obj_projection(binary):
    """
    Find circular object (such as gear in Precision Resource use case) by projection profile
    """
    kernel = np.ones((5, 5), np.uint8)
    morphology = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    bin_int = np.int64(morphology) / 255  # binary map
    # horizontal
    h_proj = bin_int.sum(1)
    sy, ey = _find_non_zero(h_proj)
    # vertical
    v_proj = bin_int.sum(0)
    sx, ex = _find_non_zero(v_proj)
    x, y, radius = np.uint16(np.around([(sx + ex) / 2, (sy + ey) / 2, max(ex - sx, ey - sy) / 2]))
    return x, y, radius

def _maybe_empty(blur_img):
    # if the std of the image is too low, it will be deemed as image without object (empty image)
    std = blur_img.std()
    return True if std < 24 else False  # usually the gear image has std > 30

def find_circular_obj_consensus(img, expected_radius = 880):
    """
    Find circular object consensus estimate of circle by combining 3 different circular object detector.

    Parameters
    ----------
    :param img: input image in numpy array, has to be (h, w, c) uint8 format
    :param expected_radius: expected radius of the circular object
    -------
    :return: x,y,r - center (x,y) of the circular object, and radius r
    """
    img_g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(img_g, (5, 5), 0)
    maybe_empty = _maybe_empty(blur)
    _, binary = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    rh, rc, rp = None, None, None
    if not maybe_empty:  # when the image is empty, find_circular_obj_hough and find_circular_obj_contour becomes very slow
        xh, yh, rh = find_circular_obj_hough(blur, 3, 2000, 100, 100, expected_radius-10, expected_radius+10)
        xc, yc, rc = find_circular_obj_contour(binary)
    xp, yp, rp = find_circular_obj_projection(binary)

    x = []
    y = []
    r = []

    if rh is not None and expected_radius + 10 > rh > expected_radius - 10:
        x.append(xh)
        y.append(yh)
        r.append(rh)
    if rc is not None and expected_radius + 10 > rc > expected_radius - 10:
        x.append(xc)
        y.append(yc)
        r.append(rc)
    if rp is not None and expected_radius + 10 > rp > expected_radius - 10:
        x.append(xp)
        y.append(yp)
        r.append(rp)
    if len(x) == 0:
        raise Exception("Circular object cannot be found")
    return np.uint16(np.around([sum(x)/len(x), sum(y)/len(y), sum(r)/len(r)]))

def _compute_center_of_bbox(sx, sy, ex, ey):
    return int((sx + ex) / 2), int((sy + ey) / 2)

def banded_polar_transform(img, center, band=(1000, 1288), padding=96, res_scale=1):
    """
    Cut a concentric band from a circular object in the input image. This is for defect detection on circular objects (e.g. gears).
    First apply polar transform, then crop a vertical stripe with the band spec,
    and lastly crop the head part (specified by padding) of this band image and append it to the end of the band.

    Parameters
    ----------
    :param img: input image in numpy array, has to be (h, w, c) uint8 format
    :param center: center of the circular object (e.g. gear), in (x, y) int format
    :param band: the crop band (currently default value is hard coded, assuming 2048x2048 image resolution)
    :param padding: the height of the padding image that cut from the head of the transformed image
    :param res_scale: scaling of angular resolution. This is for increasing the resolution for the angular direction
    Returns
    -------
    :return: transformed image, and the metadata needed for inverse transform
    """
    cx, cy = center
    row, col = img.shape[0], img.shape[1]
    max_radius = int(np.sqrt(row**2+col**2)/2)
    polar = cv2.warpPolar(img, (col, row * res_scale), (cx, cy), max_radius, cv2.WARP_FILL_OUTLIERS)
    polar = polar[:, band[0]:band[1]]
    polar = cv2.rotate(polar, cv2.ROTATE_90_COUNTERCLOCKWISE)
    padding_img = polar[:, :padding]
    polar = np.hstack((polar, padding_img))
    return polar, {"transform_type": "polar_transform", "orig_img_dim": (img.shape[1], img.shape[0]),
                   "transformed_img_dim": (polar.shape[1], polar.shape[0]), "center":(cx, cy),
                    "radius": max_radius, "band":band, "padding":padding, "res_scale": res_scale}

def inverse_banded_polar_transform(polar_img, metad, background_fill_val=0):
    """
    Inverse transform of the above banded polar transform. Currently it only supports 1-channel image for mask generation
    with polar-transformed defect detection
    Parameters
    ----------
    :param polar_img: the banded polar image obtained from the get_banded_polar_image function. This has to be gray image with (h, w) uint8 format
    :param metad: meta data returned from the get_banded_polar_image function
    :param background_fill_val: background fill value
    Returns
    -------
    :return: the recovered concentric band image, in (h, w) uint8 format
    """
    orig_img_dim = metad["orig_img_dim"]
    center = metad["center"]
    max_radius = metad["radius"]
    band = metad["band"]
    padding = metad["padding"]
    res_scale = metad["res_scale"]
    res_img = np.ones((orig_img_dim[1] * res_scale, orig_img_dim[0]), dtype=np.uint8) * background_fill_val
    # merge padding image
    padding_img = polar_img[:, -padding:]
    polar_img[:, :padding] = padding_img
    polar = polar_img[:, :-padding]
    # put into band
    polar = cv2.rotate(polar, cv2.ROTATE_90_CLOCKWISE)
    res_img[:, band[0]:band[1]] = polar
    res_img = cv2.warpPolar(res_img, (orig_img_dim[0], orig_img_dim[1]), (center[0], center[1]), max_radius, cv2.WARP_FILL_OUTLIERS + cv2.WARP_INVERSE_MAP)
    return res_img
