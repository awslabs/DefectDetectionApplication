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

def get_affine_aligned_image(src_image, tgt_image):
    '''
        align the src_image towards tgt_image such that the main object
        in src_image has the same pose as that in tgt_image 
    '''
    # feature_detector = cv2.xfeatures2d.SIFT_create()
    # feature_detector = cv2.xfeatures2d.SURF_create(400)
    feature_detector = cv2.ORB_create()
    img1 = np.uint8(src_image)
    img2 = np.uint8(tgt_image)

    # find the keypoints and descriptors with feature detector
    kp1, des1 = feature_detector.detectAndCompute(img1,None)
    kp2, des2 = feature_detector.detectAndCompute(img2,None)

    FLANN_INDEX_KDTREE = 0
    index_params = dict(algorithm = FLANN_INDEX_KDTREE, trees = 5)
    search_params = dict(checks = 50)
    flann = cv2.FlannBasedMatcher(index_params, search_params)
    matches = () if des1 is None or des2 is None else flann.knnMatch(np.float32(des1),np.float32(des2),k=2)
    # store all the good matches as per Lowe's ratio test.
    good = []
    for m,n in matches:
        if m.distance < 0.7*n.distance:
            good.append(m)
    
    MIN_MATCH_COUNT = 10
    if len(good)>MIN_MATCH_COUNT:
        pts = []
        for m in good: 
            pts.append(kp1[m.queryIdx].pt)
        src_pts = np.float32(pts).reshape(-1,1,2)
        pts = []
        for m in good: 
            pts.append(kp2[m.trainIdx].pt)
        dst_pts = np.float32(pts).reshape(-1,1,2)

        #M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC,5.0)
        M, mask = cv2.estimateAffinePartial2D(src_pts, dst_pts)
        matchesMask = mask.ravel().tolist()

        h,w, _ = img1.shape
        aligned_src_img = cv2.warpAffine(img1, M, (w,h))
    else:
        aligned_src_img = src_image
    return aligned_src_img
