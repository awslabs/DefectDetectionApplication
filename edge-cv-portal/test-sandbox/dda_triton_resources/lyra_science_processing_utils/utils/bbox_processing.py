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

def bbox_merge_nms(bboxes, scores, clses, score_thr, iou_threshold = 0.15):
    '''
    bounding box non-maximum suppression
    Args:
        bboxes - bounding boxes, each row - [sx, sy, ex, ey]
        scores - detection scores
        clses - classes 
        threshold - iou threshold for keeping the bboxes
        nms_or_merge - 0: use nms, 1: use bbox merging
    Returns:
        bboxes - processed bboxes
        scores - processed scores
        clses - classes 
    '''
    if scores.size < 1:
        return bboxes, scores, clses
    
    sel = np.where(scores >= score_thr)[0]
    if sel.size == 0:
        return np.array([]), np.array([]), np.array([])

    bboxes, scores, clses = bboxes[sel,:], scores[sel], clses[sel]

    sxs = bboxes[:, 0]
    sys = bboxes[:, 1]
    exs = bboxes[:, 2]
    eys = bboxes[:, 3]
    areas = (exs - sxs + 1) * (eys - sys + 1)
    order = np.argsort(scores.squeeze())

    picked_idx = []
    while order.size > 0:
        idx = int(order[-1])
        #picked.append(bboxes[idx])
        #picked_sore.append(scores[idx])
        picked_idx.append(idx)

        # compute intersection of union
        isx = np.maximum(sxs[idx], sxs[order[:-1]])
        iex = np.minimum(exs[idx], exs[order[:-1]])
        isy = np.maximum(sys[idx], sys[order[:-1]])
        iey = np.minimum(eys[idx], eys[order[:-1]])

        # area of intersection
        iw = np.maximum(0.0, iex - isx + 1)
        ih = np.maximum(0.0, iey - isy + 1)
        intersection = iw * ih 

        # IOU 
        iou = intersection / (areas[idx] + areas[order[:-1]] - intersection)
        keep = np.where(iou <= iou_threshold)
        order = order[keep]
    return bboxes[picked_idx, :], scores[picked_idx], clses[picked_idx]

def bbox_merge_join(bboxes, scores, clses, score_thr, iou_thr):
    '''
    bounding box merging by joining nearby high-confidence boxes
    Args:
        bboxes - bounding boxes, each row - [sx, sy, ex, ey]
        scores - detection scores
        clses - classes 
        threshold - iou threshold for keeping the bboxes
        nms_or_merge - 0: use nms, 1: use bbox merging
    Returns:
        bboxes - processed bboxes
        scores - processed scores
    '''
    if scores.size < 1:
        return bboxes, scores, clses

    sel = np.where(scores >= score_thr)[0]
    if sel.size == 0:
        return np.array([]), np.array([]), np.array([])

    bboxes, scores, clses = bboxes[sel,:], scores[sel], clses[sel]
    sxs = bboxes[:, 0]
    sys = bboxes[:, 1]
    exs = bboxes[:, 2]
    eys = bboxes[:, 3]
    areas = (exs - sxs + 1) * (eys - sys + 1)
    order = np.argsort(scores)

    box_arr, score_arr, cls_arr = [], [], []
    while order.size > 0:
        idx = order[-1]

        # compute intersection of union
        isx = np.maximum(sxs[idx], sxs[order[:-1]])
        iex = np.minimum(exs[idx], exs[order[:-1]])
        isy = np.maximum(sys[idx], sys[order[:-1]])
        iey = np.minimum(eys[idx], eys[order[:-1]])

        # area of intersection
        iw = np.maximum(0.0, iex - isx + 1)
        ih = np.maximum(0.0, iey - isy + 1)
        intersection = iw * ih 

        sx, ex, sy, ey = sxs[idx], exs[idx], sys[idx], eys[idx]
        join_idx = np.where(intersection > 0)
        if join_idx[0].size > 0:
            sx = min(sx, np.min(sxs[order[:-1]][join_idx]))
            ex = max(ex, np.max(exs[order[:-1]][join_idx]))
            sy = min(sy, np.min(sys[order[:-1]][join_idx]))
            ey = max(ey, np.max(eys[order[:-1]][join_idx]))

        box_arr.append([sx, sy, ex, ey])
        score_arr.append(scores[idx])
        cls_arr.append(clses[idx])

        iou = intersection / (areas[idx] + areas[order[:-1]] - intersection)
        keep = np.where(iou <= iou_thr)
        # keep = np.where(intersection <= 0)
        order = order[keep]
    bboxes, scores, clses = np.array(box_arr), np.array(score_arr), np.array(cls_arr)
    return bboxes, scores, clses

def bbox_merge_join_recursive(bboxes, scores, clses, score_thr, iou_thr):
    num, old_num = 0, -1
    while num!=old_num:
        bboxes, scores, clses = bbox_merge_join(bboxes, scores, clses, score_thr, iou_thr)
        old_num = num
        num = scores.shape[0] if len(scores) > 0 else 0
    return bboxes, scores, clses

def bbox_post_processing(bboxes, scores, clses, bbox_cls=None, pp_type='merge', score_thr=0.1, iou_thr=0.15):
    '''
    Args: 
        bboxes - input bboxes, scores - bbox scores, clses - bbox class from vanilla CenterNet, 
        bbox_cls - bbox class from ClassHead,  pp_type - postprocessing type, 'merge' or 'nms'
        score_thr - score threshold,  iou_thr - iou thershold used in merge and nms
    Return:
        the post-processed bboxes
    '''
    bboxes, scores, clses = bboxes[0], scores[0].squeeze(), clses[0].squeeze()
    bbox_cls = bbox_cls[0].squeeze() if bbox_cls is not None else None 

    cls_indices = np.unique(clses)
    if bbox_cls is None:  # use regular centernet class output
        bboxes_out, scores_out, clses_out =  np.empty((0,4)),np.empty((0)),np.empty((0))
        for cls_idx in cls_indices:
            index = np.where(clses==cls_idx)[0]
            _bboxes = bboxes[index, :]
            _scores = scores[index]
            _clses  = clses[index]
            _bboxes, _scores, _clses = bbox_merge_join_recursive(_bboxes, _scores, _clses, score_thr, iou_thr)
            if len(_clses)>0:
                bboxes_out = np.concatenate((bboxes_out, _bboxes), axis=0)
                scores_out = np.concatenate((scores_out, _scores), axis=0)
                clses_out = np.concatenate((clses_out, _clses), axis=0)
    else:  # use class head output
        clses = bbox_cls.argmax(axis=1)  if len(bbox_cls.shape)==2 else np.zeros(bbox_cls.shape[0])
        if pp_type=='merge':
            bboxes_out, scores_out, clses_out = bbox_merge_join_recursive(bboxes, scores, clses, score_thr, iou_thr)
        else:
            bboxes_out, scores_out, clses_out = bbox_merge_nms(bboxes, scores, clses, score_thr, iou_thr)

    return bboxes_out, scores_out, clses_out
