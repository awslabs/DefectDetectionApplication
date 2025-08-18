#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
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
import os
import json
import glob
import base64
import time
import math
from typing import List

from datetime import datetime, timezone
from dateutil import parser
from fastapi import HTTPException

from utils import utils
from utils import constants
from utils.feature_configs_utils import get_default_configs_lfv


class GetInferenceResults():
    def __init__(self, stream_id, sort, starting_point, max_results):
        self.EM_AGENT_CONFIG_PATH = utils.get_em_agent_config_path_for_stream(stream_id)
        self.stream_id = stream_id

        self.sort = sort
        self.starting_point = starting_point
        self.max_results = max_results

        
        self.img_skip_count = 0
        self.img_out_count = 0

    def read_raw_data(self, read_file_path):
        time_to_wait = 10
        time_counter = 0
        without_prefix = utils.remove_prefix(read_file_path, "file://")
        while not os.path.exists(without_prefix):
            time.sleep(1)
            time_counter += 1
            if time_counter > time_to_wait:
                raise Exception("Inference output not created on path: {}".format(without_prefix))
        return utils.get_image_bytes_from_file(without_prefix)

    def save_image_object(self, json_line, jsonl_filepath, capture_id=None):
        input_image_file_path = ""
        if json_line.get("deviceFleetAuxiliaryOutputs") and len(json_line["deviceFleetAuxiliaryOutputs"]) > 0:
            output_list = json_line.get("deviceFleetAuxiliaryOutputs")
            input_list = json_line["deviceFleetAuxiliaryInputs"]
            if is_segmentation_model_output_result(output_list):
                # Segmentation model
                image_data_file_path = get_data_for_content_type(output_list, constants.INFERENCE_OUTPUT_IMAGE_CONTENT_TYPE, "data-ref") or \
                                    get_data_for_content_type(output_list, constants.INFERENCE_OUTPUT_OVERLAY_CONTENT_TYPE, "data-ref")
                input_image_file_path = get_data_for_content_type(input_list, constants.INFERENCE_INPUT_IMAGE_CONTENT_TYPE, "data-ref")
                mask_image = self.get_mask_base64_image(output_list)
                base64_res = get_data_for_content_type(output_list, constants.INFERENCE_OUTPUT_RES_CONTENT_TYPE, "data")
                base64_lab = get_data_for_content_type(output_list, constants.INFERENCE_OUTPUT_RES_LABEL_CONTENT_TYPE, "data")
                dict_res = json.loads(base64.b64decode(base64_res))
                dict_lab = json.loads(base64.b64decode(base64_lab))
                mask_background = dict_lab["anomalies"]["0"]
                del dict_lab["anomalies"]["0"]

                infer_res = {
                    # temp backend fix for confidence score
                    # 'confidence': dict_res["Confidence"],
                    'confidence': self.temp_get_confidence(dict_res),
                    'inference_result': dict_res["Inference result"],
                    'anomaly_score': dict_res.get("Anomaly_score"),
                    'anomaly_threshold': dict_res.get("Anomaly_threshold"),
                }
                if mask_image:
                    infer_res["mask_background"] = convert_hex_color_to_rgb(mask_background)
                    infer_res["mask_image"] = mask_image
                    infer_res["anomalies"] = dict_lab["anomalies"]
            else:
                # Classification model
                image_data_file_path = get_data_for_content_type(input_list, constants.INFERENCE_INPUT_IMAGE_CONTENT_TYPE, "data-ref")
                input_image_file_path = image_data_file_path
                base64_res = get_data_for_content_type(output_list, constants.INFERENCE_OUTPUT_RES_CONTENT_TYPE, "data")
                dict_res = json.loads(base64.b64decode(base64_res))
                infer_res = {
                    # temp backend fix for confidence score
                    # 'confidence': dict_res["Confidence"],
                    'confidence': self.temp_get_confidence(dict_res),
                    'anomaly_score': dict_res.get("Anomaly_score"),
                    'anomaly_threshold': dict_res.get("Anomaly_threshold"),
                    'inference_result': dict_res["Inference result"]
                }
        else:
            raise Exception(f"Invalid deviceFleetAuxiliaryOutputs found in {jsonl_filepath}")

        image_data_file_path = utils.remove_prefix(image_data_file_path, "file://")
        input_image_file_path = utils.remove_prefix(input_image_file_path, "file://")

        result_event_metadata = json_line["eventMetadata"]

        image_object = {
            'imageDataFilePath': image_data_file_path,
            'inputImageFilePath': input_image_file_path,
            'creationTime': result_event_metadata["inferenceTime"],
            'inferenceResult': infer_res,
            'inferenceFilePath': jsonl_filepath,
            'image': self.read_raw_data(image_data_file_path),
            # humanReviewRequired uses confidence thresholds instead of anomaly score, thats why we pass in the
            # returned confidence score instead of the converted one based on result.
            'humanReviewRequired' : self.human_review_required(result_event_metadata, dict_res.get("Confidence"),
                                                               dict_res["Inference result"])
        }

        if capture_id:
            image_object["captureId"] = capture_id
        return image_object

    def human_review_required(self, result_event_metadata, inference_confidence, inference_result):
        model = get_default_configs_lfv(result_event_metadata["modelName"])
        model_thresholds = model.get(constants.MODEL_CONFIDENCE_THRESHOLDS)

        # Backwards compatibility - older models will always show False
        if not model_thresholds:
            return False

        threshold_to_use = float(model_thresholds.get(constants.MODEL_CONFIDENCE_THRESHOLD_NORMAL)) if inference_result == constants.NORMAL \
            else float(model_thresholds.get(constants.MODEL_CONFIDENCE_THRESHOLD_ANOMALY))

        return inference_confidence < threshold_to_use

    def read_image_from_jsonl(self, jsonl_filepath, capture_id=None):
        with open(jsonl_filepath, 'r') as jsonl_file:
            json_list = list(jsonl_file)

        # sorting
        sorted_jsonl = []
        for idx, json_line in enumerate(json_list):
            json_str = json.loads(json_line)
            time_str = json_str["eventMetadata"]["inferenceTime"]
            if "_" in time_str:
                time_str, differentiator = time_str.split("_")
            else:
                differentiator = ""
            infer_time = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S")
            sorted_jsonl.append((idx, infer_time, differentiator))
        sorted_jsonl = sorted(sorted_jsonl, key=lambda json_line: (json_line[1], json_line[2]),
                              reverse=False if self.sort == 'asc' else True)

        # pagination
        images = []
        if self.img_skip_count < self.starting_point:
            skip_cnt = self.starting_point - self.img_skip_count
            if skip_cnt >= len(sorted_jsonl):
                self.img_skip_count += len(sorted_jsonl)
                return []
            else:
                sorted_jsonl = sorted_jsonl[skip_cnt:]
                self.img_skip_count += skip_cnt
        if len(sorted_jsonl) <= self.max_results - self.img_out_count:
            save_cnt = len(sorted_jsonl)
        else:
            save_cnt = self.max_results - self.img_out_count

        for idx, dt, diff in sorted_jsonl[:save_cnt]:
            json_line = json.loads(json_list[idx])
            images.append(self.save_image_object(json_line, jsonl_filepath, capture_id))
        self.img_out_count += save_cnt

        return images

    # To get last N images, return image as base64 string
    def get_inference_results(self, workflow_output_path):
        jsonl_files = list(filter(os.path.isfile, glob.glob(workflow_output_path + "/*.jsonl")))
        jsonl_files = sorted(jsonl_files, key=lambda x: os.path.getmtime(x),
                             reverse=False if self.sort == 'asc' else True)
        output_images = []
        for jsonl in jsonl_files:
            if self.img_out_count < self.max_results:
                output_images += self.read_image_from_jsonl(jsonl)
            else:
                break

        if self.img_out_count < self.max_results:
            return {"images": output_images}
        else:
            return {"images": output_images, "nextStartingPoint": self.starting_point + self.max_results}

    def read_inference_result_from_jsonl(self, jsonl_content, jsonl_filepath):
        images = []
        for idx, json_line in enumerate(jsonl_content):
            json_line = json.loads(jsonl_content[idx])
            images.append(self.save_image_object(json_line, jsonl_filepath))
        return images

    def get_infer_res_with_capture_id(self, capture_id, workflow_output_path):
        jsonl_file = workflow_output_path + "/" + capture_id + ".jsonl"
        output_images = self.read_image_from_jsonl(jsonl_file, capture_id)

        return output_images[0]

    def get_mask_base64_image(self, output_list):
        '''There will be 2 types of mask from inference output:
        1. mask image encoded in base64 string directly
        2. mask image file path
        It depends on `sagemaker_edge_core_capture_data_base64_embed_limit`, default limit 3KB
        if larger than 3KB will be image file path, otherwise, will be base64 string directly
        '''
        base64_image = None
        for mask in output_list:
            if mask["observedContentType"].startswith(constants.INFERENCE_OUTPUT_MASK_CONTENT_TYPE_PREFIX):
                if mask.get("data"):
                    base64_image = mask["data"]
                else:
                    file_path = mask["data-ref"]
                    file_path = utils.remove_prefix(file_path, "file://")
                    base64_image = self.read_raw_data(file_path)
        return base64_image

    def temp_get_confidence(self, dict_res):
        label = dict_res.get("Inference result")
        anomaly_score = dict_res.get("Anomaly_score")
        confidence = dict_res.get("Confidence")
        if label == constants.NORMAL and math.isclose(anomaly_score, confidence):
            return 1 - anomaly_score
        else:
            return confidence

# General inference result util functions
def is_segmentation_model_output_result(json_output: list):
    '''Determine if this inference result for segmentation model or not
    Input: value for key `deviceFleetAuxiliaryOutputs` in result file
    Output: True -> segmentation, False -> classification
    '''
    for output_data in json_output:
        if output_data["observedContentType"] == constants.INFERENCE_OUTPUT_IMAGE_CONTENT_TYPE or \
            output_data["observedContentType"].startswith(constants.INFERENCE_OUTPUT_MASK_CONTENT_TYPE_PREFIX):
            return True
    return False

def get_data_for_content_type(json_list: list, content_type: str, key: str):
    for data in json_list:
        if data["observedContentType"] == content_type:
            return data[key]
    return None

def convert_hex_color_to_rgb(mask_background):
    mask = {}
    for key in mask_background:
        if key == "hex-color":
            hex_color = mask_background[key].lstrip('#')
            mask["rgb-color"] = [int(hex_color[i:i+2], 16) for i in (0, 2, 4)]
        else:
            mask[key] = mask_background[key]
    return mask

def convert_inference_res_to_save_in_db(inference_res, workflow):
    model_config = get_default_configs_lfv(workflow.get("featureConfigurations")[0].get("modelName"))

    db_inference_res = {}
    db_inference_res["workflowId"] = workflow.get("workflowId")
    db_inference_res["modelId"] = workflow.get("featureConfigurations")[0].get("modelName")
    db_inference_res["modelName"] = model_config.get("modelAlias")
    db_inference_res["captureId"] = inference_res["captureId"]
    db_inference_res["captureType"] = inference_res["captureType"]
    db_inference_res["inputImageFilePath"] = inference_res["inputImageFilePath"]
    db_inference_res["outputImageFilePath"] = inference_res["imageDataFilePath"]
    db_inference_res["inferenceCreationTime"] = int(parser.parse(inference_res["creationTime"]).timestamp())
    db_inference_res["confidence"] = inference_res["inferenceResult"]["confidence"]
    db_inference_res["anomalyScore"] = inference_res["inferenceResult"]["anomaly_score"]
    db_inference_res["anomalyThreshod"] = inference_res["inferenceResult"]["anomaly_threshold"]
    db_inference_res["prediction"] = inference_res["inferenceResult"]["inference_result"]
    db_inference_res["maskImage"] = inference_res["inferenceResult"].get("mask_image")
    db_inference_res["maskBackground"] = inference_res["inferenceResult"].get("mask_background")
    db_inference_res["flagForReview"] = False
    db_inference_res["downloaded"] = False
    db_inference_res["humanClassification"] = None
    db_inference_res["textNote"] = None
    db_inference_res["humanReviewRequired"] = inference_res["humanReviewRequired"]
    db_inference_res["modelConfidenceThresholds"] = model_config.get("modelConfidenceThresholds")

    anomaly_labels = inference_res["inferenceResult"].get("anomalies")
    if anomaly_labels:
        db_inference_res["anomalyLabels"] = [anomaly_labels[key] for key in anomaly_labels]

    # Remove all None values to match schema
    db_inference_res_schema = {k: v for k, v in db_inference_res.items() if v is not None}
    return db_inference_res_schema

def generate_smgt_format_manifest(inference_results_data_list: List[dict]):
    manifest_data = []
    for data in inference_results_data_list:
        # https://docs.aws.amazon.com/lookout-for-vision/latest/developer-guide/manifest-file-classification.html
        is_human_annotated = True if data["humanClassification"] else False
        classification = data["humanClassification"] if is_human_annotated else data["prediction"]

        anomaly_label_metadata = {}
        anomaly_label_metadata["class-name"] = classification
        anomaly_label_metadata["confidence"] = data["confidence"]
        anomaly_label_metadata["type"] = "groundtruth/image-classification"
        anomaly_label_metadata["human-annotated"] = "yes" if is_human_annotated else "no"
        anomaly_label_metadata["creation-date"] = convert_timestamp(data["inferenceCreationTime"])

        to_add = {}
        to_add["source-ref"] = os.path.basename(data["inputImageFilePath"])
        to_add["source-ref-metadata"] = {"notes": data["textNote"]}
        to_add["anomaly-label"] = 1 if classification == constants.ANOMALY else 0
        to_add["anomaly-label-metadata"] = anomaly_label_metadata
        manifest_data.append(to_add)

    return manifest_data

def convert_timestamp(timestamp):
    # Check if the timestamp is 10 digits or 13 digits
    if len(str(timestamp)) == 10:
        dt = datetime.fromtimestamp(timestamp, timezone.utc)
    elif len(str(timestamp)) == 13:
        dt = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
    else:
        raise ValueError("Timestamp must be either 10 or 13 digits")

    return dt.strftime("%Y-%m-%dT%H:%M:%S")
