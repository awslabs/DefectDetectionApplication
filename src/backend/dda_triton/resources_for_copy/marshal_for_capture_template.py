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

import numpy as np
import time
import os
import logging
import time
import json
import triton_python_backend_utils as pb_utils
import base64
import cv2

DEFAULT_CONFIDENCE_WATERMARK = 0.6
DEFAULT_ANOMALY_THRESHOLD = 0.0

log = logging.getLogger(__name__)


class TritonPythonModel:
    """Your Python model must use the same class name. Every Python model
    that is created must have "TritonPythonModel" as the class name.
    """

    def initialize(self, args):
        """`initialize` is called only once when the model is being loaded.
        Implementing `initialize` function is optional. This function allows
        the model to initialize any state associated with this model.

        Parameters
        ----------
        args : dict
          Both keys and values are strings. The dictionary keys and values are:
          * model_config: A JSON string containing the model configuration
          * model_instance_kind: A string containing model instance kind
          * model_instance_device_id: A string containing model instance device ID
          * model_repository: Model repository path
          * model_version: Model version
          * model_name: Model name
        """
        # Get all input configurations
        self.model_config = model_config = json.loads(args["model_config"])
        self.model_name = model_config.get("name", "")
        marshal_prefix = "marshal_"
        if self.model_name.startswith(marshal_prefix):
            self.model_name = self.model_name[len(marshal_prefix) :]
        self.model_version = str(args["model_version"])
        input_config = pb_utils.get_input_config_by_name(model_config, "input")
        self.input_dtype = pb_utils.triton_string_to_numpy(input_config["data_type"])
        metadata_config = pb_utils.get_input_config_by_name(model_config, "metadata")
        self.metadata_dtype = pb_utils.triton_string_to_numpy(metadata_config["data_type"])
        inf_config = pb_utils.get_input_config_by_name(model_config, "inference_output")
        self.inf_dtype = pb_utils.triton_string_to_numpy(inf_config["data_type"])
        mask_config = pb_utils.get_input_config_by_name(model_config, "inference_mask")
        self.mask_dtype = pb_utils.triton_string_to_numpy(mask_config["data_type"])
        score_config = pb_utils.get_input_config_by_name(model_config, "inference_score")
        self.score_dtype = pb_utils.triton_string_to_numpy(score_config["data_type"])
        confidence_config = pb_utils.get_input_config_by_name(model_config, "inference_confidence")
        self.confidence_dtype = pb_utils.triton_string_to_numpy(confidence_config["data_type"])
        anomalies_config = pb_utils.get_input_config_by_name(model_config, "inference_anomalies")
        self.anomalies_dtype = pb_utils.triton_string_to_numpy(anomalies_config["data_type"])
        # Get all output configurations.
        output_config = pb_utils.get_output_config_by_name(model_config, "output")
        self.output_dtype = pb_utils.triton_string_to_numpy(output_config["data_type"])
        output_mask_config = pb_utils.get_output_config_by_name(model_config, "mask")
        self.output_mask_dtype = pb_utils.triton_string_to_numpy(output_mask_config["data_type"])
        output_overlay_config = pb_utils.get_output_config_by_name(model_config, "overlay")
        self.output_overlay_dtype = pb_utils.triton_string_to_numpy(
            output_overlay_config["data_type"]
        )
        output_anomalous = pb_utils.get_output_config_by_name(model_config, "output_anomalous")
        self.output_anomalous_dtype = pb_utils.triton_string_to_numpy(output_anomalous["data_type"])
        output_confidence = pb_utils.get_output_config_by_name(model_config, "output_confidence")
        self.output_confidence_dtype = pb_utils.triton_string_to_numpy(
            output_confidence["data_type"]
        )

    def _get_time_str(self):
        current_time = time.time()
        time_str = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(current_time))
        return time_str

    def _generate_overlay(self, image, mask):
        # get alpha and find all non-(255,255,255) pixels in mask.
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        idx_alpha = np.where(np.any(mask != [255, 255, 255], axis=-1))
        image[idx_alpha[0], idx_alpha[1], :] = image[idx_alpha[0], idx_alpha[1], :] * 0.5
        mask[idx_alpha[0], idx_alpha[1], :] = mask[idx_alpha[0], idx_alpha[1], :] * 0.5
        return mask + image

    def _generate_capture_meta_data(
        self,
        capture_meta_data,
        inference_output,
        time_str,
        inference_confidence,
        inference_mask,
        inference_anomalies,
        inference_score,
        input_image,
    ):
        ret = {}
        ret["deviceGroundTruthData"] = []
        ret["deviceGroundTruthData"].append({})
        idx = 0
        capture_id = capture_meta_data["capture_id"]
        workflow_id = capture_meta_data["workflow_id"]
        input_file_path = ""
        if capture_meta_data["capture_folder"] and capture_meta_data["capture_id"]:
            input_file_path = os.path.join(capture_meta_data["capture_folder"], f"{capture_id}.jpg")
            ret["deviceGroundTruthData"][idx]["source-ref"] = os.path.join(
                "file:/", input_file_path
            )

        class_name = ""
        if inference_output:
            ret["deviceGroundTruthData"][idx]["anomaly-label-detected"] = 1
            class_name = "Anomaly"
        else:
            ret["deviceGroundTruthData"][idx]["anomaly-label-detected"] = 0
            class_name = "Normal"
        label_detected_metadata = {}
        label_detected_metadata["class-name"] = class_name
        label_detected_metadata["creation-date"] = time_str
        label_detected_metadata["human-annotated"] = "no"
        label_detected_metadata["type"] = "groundtruth/image-classification"
        label_detected_metadata["confidence"] = inference_confidence.astype(float)
        ret["deviceGroundTruthData"][idx][
            "anomaly-label-detected-metadata"
        ] = label_detected_metadata
        mask_file_path = ""
        if self._has_anomaly_mask(inference_mask, input_image):
            mask_file_path = os.path.join(
                capture_meta_data["capture_folder"], f"{capture_id}.mask.png"
            )
            ret["deviceGroundTruthData"][idx]["anomaly-mask-ref-detected"] = os.path.join(
                "file:/", mask_file_path
            )
            anomaly_mask_ref_detected_meta = {}
            d = {}
            for i in range(len(inference_anomalies)):
                detail = {}
                detail["name"] = inference_anomalies[i]["name"]
                detail["hex-color"] = inference_anomalies[i]["hex_color"].lower()
                detail["total-percentage-area"] = inference_anomalies[i]["total_percentage_area"]
                d[str(i)] = detail
            anomaly_mask_ref_detected_meta["internal-color-map"] = d
            anomaly_mask_ref_detected_meta["creation-date"] = time_str
            anomaly_mask_ref_detected_meta["human-annotated"] = "no"
            anomaly_mask_ref_detected_meta["type"] = "groundtruth/semantic-segmentation"
            anomaly_mask_ref_detected_meta["job-name"] = "labeling-job/segmentation-job"
            ret["deviceGroundTruthData"][idx][
                "anomaly-mask-ref-detected-metadata"
            ] = anomaly_mask_ref_detected_meta
        # fill in auxiliary data
        ret["deviceFleetAuxiliaryInputs"] = []
        ret["deviceFleetAuxiliaryOutputs"] = []

        # auxiliary data
        if input_file_path:
            ret["deviceFleetAuxiliaryInputs"].append(
                {
                    "data-ref": f"file://{input_file_path}",
                    "encoding": "NONE",
                    "observedContentType": "jpg",
                }
            )
        if mask_file_path:
            ret["deviceFleetAuxiliaryOutputs"].append(
                {
                    "data-ref": f"file://{mask_file_path}",
                    "encoding": "NONE",
                    "observedContentType": "mask.png",
                }
            )
        if self._has_anomaly_mask(inference_mask, input_image):
            overlay_file_path = os.path.join(
                capture_meta_data["capture_folder"], f"{capture_id}.overlay.jpg"
            )
            if overlay_file_path:
                ret["deviceFleetAuxiliaryOutputs"].append(
                    {
                        "data-ref": f"file://{overlay_file_path}",
                        "encoding": "NONE",
                        "observedContentType": "overlay.jpg",
                    }
                )
        # inference result
        inf_result = {}
        inf_result["Inference status"] = "success"
        if inference_output:
            inf_result["Inference result"] = "Anomaly"
        else:
            inf_result["Inference result"] = "Normal"
        inf_result["Confidence"] = inference_confidence.astype(float)
        inf_result["Anomaly_score"] = inference_score.astype(float)
        # default thershold not used in inference for now.
        inf_result["Anomaly_threshold"] = 1.0
        inf_result["Error msg"] = ""
        inf_result_str = json.dumps(inf_result)
        inf_result_str_encoded = base64.b64encode(inf_result_str.encode()).decode()
        ret["deviceFleetAuxiliaryOutputs"].append(
            {
                "data": inf_result_str_encoded,
                "encoding": "BASE64",
                "observedContentType": "json",
            }
        )
        # anomaly list
        anomalies = inference_anomalies
        d = {}
        for i, anomaly in enumerate(anomalies):
            detail = {
                "class-name": anomaly["name"],
                "hex-color": anomaly["hex_color"].lower(),
                "total-percentage-area": anomaly["total_percentage_area"],
            }
            d[str(i)] = detail

        if anomalies:
            anomaly_data = {"anomalies": d}
            anomaly_str = json.dumps(anomaly_data)
            anomaly_str_encoded = base64.b64encode(anomaly_str.encode()).decode()
            ret["deviceFleetAuxiliaryOutputs"].append(
                {
                    "data": anomaly_str_encoded,
                    "encoding": "BASE64",
                    "observedContentType": "json_with_base64_encoding",
                }
            )

        # meta data
        ret["eventMetadata"] = {
            "capture_folder": capture_meta_data.get("capture_folder", ""),
            "eventId": capture_meta_data.get("event_id", ""),
            "deviceFleetName": capture_meta_data.get("device_fleet_name", ""),
            "modelName": self.model_name,
            "modelVersion": self.model_version,
            "inferenceTime": time_str,
        }
        ret["eventVersion"] = "0"
        return ret

    def _has_anomalies(self, anomalies) -> bool:
        # any anomalies?
        return len(anomalies) != 0

    def _encode_mask(self, mask):
        ret = np.array([], dtype=self.output_mask_dtype)
        enc = cv2.imencode(".png", mask)
        if not enc[0]:
            logging.error("Unable to encode mask for output")
            return ret
        return enc[1]

    def _encode_overlay(self, overlay):
        ret = np.array([], dtype=self.output_overlay_dtype)
        enc = cv2.imencode(".jpg", overlay)
        if not enc[0]:
            logging.error("Unable to encode overlay for output")
            return ret
        return enc[1]

    def _has_anomaly_mask(self, inference_mask, input_image):
        return np.any(inference_mask) and input_image.shape == inference_mask.shape

    def execute(self, requests):
        """`execute` MUST be implemented in every Python model. `execute`
        function receives a list of pb_utils.InferenceRequest as the only
        argument. This function is called when an inference request is made
        for this model. Depending on the batching configuration (e.g. Dynamic
        Batching) used, `requests` may contain multiple requests. Every
        Python model, must create one pb_utils.InferenceResponse for every
        pb_utils.InferenceRequest in `requests`. If there is an error, you can
        set the error argument when creating a pb_utils.InferenceResponse

        Parameters
        ----------
        requests : list
          A list of pb_utils.InferenceRequest

        Returns
        -------
        list
          A list of pb_utils.InferenceResponse. The length of this list must
          be the same as `requests`
        """
        responses = []

        # Every Python backend must iterate over everyone of the requests
        # and create a pb_utils.InferenceResponse for each of them.
        for request in requests:
            # Get input tensors
            input1 = pb_utils.get_input_tensor_by_name(request, "input")
            inference_output = pb_utils.get_input_tensor_by_name(request, "inference_output")
            inference_mask = pb_utils.get_input_tensor_by_name(request, "inference_mask")
            inference_score = pb_utils.get_input_tensor_by_name(request, "inference_score")
            inference_confidence = pb_utils.get_input_tensor_by_name(
                request, "inference_confidence"
            )
            inference_anomalies = pb_utils.get_input_tensor_by_name(request, "inference_anomalies")
            capture_meta_data = pb_utils.get_input_tensor_by_name(request, "metadata")
            time_str = self._get_time_str()
            inference_anomalies = inference_anomalies.as_numpy()
            inference_anomalies = inference_anomalies.view(
                f"S{inference_anomalies.shape[0]}"
            ).astype("U")[0]
            capture_meta_data = capture_meta_data.as_numpy()
            capture_meta_data = json.loads(
                capture_meta_data.view(f"S{capture_meta_data.shape[0]}").astype("U")[0]
            )
            capture_meta_data["capture_folder"] = capture_meta_data[
                "sagemaker_edge_core_capture_data_disk_path"
            ]
            capture_meta_data["workflow_id"] = os.path.basename(
                os.path.normpath(capture_meta_data["sagemaker_edge_core_capture_data_disk_path"])
            )
            workflow_id = capture_meta_data["workflow_id"]
            capture_id = capture_meta_data["capture_id"]
            capture_meta_data["event_id"] = f"{capture_id}"
            capture_meta_data["device_fleet_name"] = capture_meta_data[
                "sagemaker_edge_core_device_fleet_name"
            ]
            inference_anomalies = json.loads(inference_anomalies)
            output = self._generate_capture_meta_data(
                capture_meta_data=capture_meta_data,
                inference_output=inference_output.as_numpy()[0],
                time_str=time_str,
                inference_confidence=inference_confidence.as_numpy()[0],
                inference_mask=inference_mask.as_numpy(),
                inference_anomalies=inference_anomalies,
                inference_score=inference_score.as_numpy()[0],
                input_image=input1.as_numpy(),
            )
            output_tensor = pb_utils.Tensor(
                "output",
                np.frombuffer(bytes(json.dumps(output), encoding="utf-8"), dtype=np.uint8).astype(
                    self.output_dtype
                ),
            )
            output_anomalous = pb_utils.Tensor(
                "output_anomalous", inference_output.as_numpy().astype(self.output_anomalous_dtype)
            )
            output_confidence = pb_utils.Tensor(
                "output_confidence",
                inference_confidence.as_numpy().astype(self.output_confidence_dtype),
            )
            mask_tensor = None
            overlay_tensor = None
            encoded_mask = None
            if self._has_anomaly_mask(inference_mask.as_numpy(), input1.as_numpy()):
                # encode and forward
                encoded_mask = self._encode_mask(inference_mask.as_numpy()).astype(
                    self.output_mask_dtype
                )
                mask_tensor = pb_utils.Tensor("mask", encoded_mask)
                encoded_overlay = self._encode_overlay(
                    self._generate_overlay(input1.as_numpy(), inference_mask.as_numpy())
                ).astype(self.output_overlay_dtype)
                overlay_tensor = pb_utils.Tensor("overlay", encoded_overlay)
            else:
                mask_tensor = pb_utils.Tensor("mask", np.array([]).astype(self.output_mask_dtype))
                overlay_tensor = pb_utils.Tensor(
                    "overlay", np.array([]).astype(self.output_overlay_dtype)
                )
            # Create the inference response.
            response = pb_utils.InferenceResponse(
                output_tensors=[
                    output_tensor,
                    mask_tensor,
                    overlay_tensor,
                    output_anomalous,
                    output_confidence,
                ]
            )
            responses.append(response)
        return responses

    def finalize(self):
        """`finalize` is called when the model is being unloaded from the
        server. This function is used to perform any necessary cleanup or
        finalization steps.
        """
        log.info("Cleaning up...")
