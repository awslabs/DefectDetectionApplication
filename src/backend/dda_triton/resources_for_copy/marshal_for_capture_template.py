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
import sys
import logging
import time
import json
import triton_python_backend_utils as pb_utils
import base64
import cv2

DEFAULT_CONFIDENCE_WATERMARK = 0.6
DEFAULT_ANOMALY_THRESHOLD = 0.0

log = logging.getLogger(__name__)

# Human-readable class-label resolution for object-detection captures.
#
# The shared resolver lives in the DDA app package
# (lyra_science_processing_utils). The Triton Python-backend stub does NOT
# forward the parent process's PYTHONPATH and runs with a CWD that is not the
# app root, so a bare import is otherwise unresolvable here -- exactly the
# situation lfv_model_template.py handles. Replicate that model's established
# sys.path setup (app root via DDA_APP_ROOT + this file's own dir) BEFORE
# importing so the package resolves regardless of the stub's environment.
_DDA_APP_ROOT = os.environ.get("DDA_APP_ROOT", "/")
if _DDA_APP_ROOT and _DDA_APP_ROOT not in sys.path:
    sys.path.insert(0, _DDA_APP_ROOT)
_MARSHAL_DIR = os.path.dirname(os.path.abspath(__file__))
if _MARSHAL_DIR and _MARSHAL_DIR not in sys.path:
    sys.path.insert(0, _MARSHAL_DIR)

# Even with the sys.path setup above, the import is not guaranteed on every
# hot-patched/mixed-version deployment, so guard it defensively: the marshal
# must never hard-fail at import time on label resolution. On ImportError fall
# back to a local resolver that prefers a payload-provided class_label and
# otherwise returns the class-index string.
try:
    from lyra_science_processing_utils.utils.class_label_map import (
        resolve_class_label,
    )
except ImportError:  # pragma: no cover - defensive fallback for restricted envs
    log.warning(
        "lyra_science_processing_utils.utils.class_label_map is not importable; "
        "using local class-label fallback (payload class_label else index string)."
    )

    def resolve_class_label(class_index, class_map=None):
        """Local fallback: return the mapped label if present, else the
        class_index rendered as a string. Never raises."""
        try:
            if class_map:
                if class_index in class_map:
                    return str(class_map[class_index])
                try:
                    key = int(class_index)
                    if key in class_map:
                        return str(class_map[key])
                except (TypeError, ValueError):
                    pass
        except Exception:  # noqa: BLE001 - defensive, never raise
            pass
        return str(class_index)


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
        # The palette-colored mask arrives in RGB order, but the overlay is
        # JPEG-encoded via cv2.imencode (which treats the array as BGR), and the
        # image above is channel-swapped to survive that encode. The mask was
        # NOT swapped, so its colors came out with R and B flipped relative to
        # the RGB palette / legend hex colors (olive->teal, purple->orange).
        # Swap the mask to BGR too so overlay colors match the reported legend.
        mask = cv2.cvtColor(mask, cv2.COLOR_RGB2BGR)
        idx_alpha = np.where(np.any(mask != [255, 255, 255], axis=-1))
        image[idx_alpha[0], idx_alpha[1], :] = image[idx_alpha[0], idx_alpha[1], :] * 0.5
        mask[idx_alpha[0], idx_alpha[1], :] = mask[idx_alpha[0], idx_alpha[1], :] * 0.5
        return mask + image

    @staticmethod
    def _is_detection_list(anomalies) -> bool:
        """Detect whether the (reused) anomalies payload is actually a list of
        object-detection results (each carrying a 'bounding_box'), as emitted by
        the base model for task=object_detection."""
        return (
            isinstance(anomalies, list)
            and len(anomalies) > 0
            and isinstance(anomalies[0], dict)
            and "bounding_box" in anomalies[0]
        )

    def _generate_detection_overlay(self, image, detections):
        """Draw bounding boxes + labels onto a copy of the input image."""
        # The overlay is JPEG-encoded via cv2.imencode, which treats the array
        # as BGR. The input image arrives in RGB order (same as the anomaly
        # path in _generate_overlay), so swap R<->B here before drawing/encoding
        # or the encoded overlay comes out with red and blue flipped (e.g. skin
        # tones render blue). The box/label colors below are green (0,255,0) and
        # black (0,0,0), both symmetric under an R<->B swap, so they are drawn in
        # the swapped space unchanged.
        overlay = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        h, w = overlay.shape[0], overlay.shape[1]
        for det in detections:
            box = det.get("bounding_box") or []
            if len(box) != 4:
                continue
            x1, y1, x2, y2 = (int(round(v)) for v in box)
            # Clamp to image bounds.
            x1 = max(0, min(x1, w - 1))
            x2 = max(0, min(x2, w - 1))
            y1 = max(0, min(y1, h - 1))
            y2 = max(0, min(y2, h - 1))
            class_index = str(det.get("class", ""))
            # Prefer the resolved human-readable label the base model embedded
            # in the payload; if missing/empty, re-resolve via the shared map,
            # then finally fall back to the class-index string.
            class_label = det.get("class_label") or ""
            if not class_label:
                class_label = resolve_class_label(class_index, None)
            if not class_label:
                class_label = class_index
            conf = det.get("confidence", 0.0)
            try:
                label = f"{class_label} {float(conf) * 100:.0f}%"
            except (TypeError, ValueError):
                label = class_label

            cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 255, 0), 2)

            # Draw the label + confidence in the UPPER-RIGHT corner of the box,
            # on a filled band for readability (green background, black text),
            # right-aligned to the box's right edge and clamped to the image.
            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 0.5
            font_thickness = 1
            (text_w, text_h), baseline = cv2.getTextSize(
                label, font, font_scale, font_thickness
            )
            pad = 3
            band_h = text_h + baseline + 2 * pad
            band_x2 = min(w, x2)
            band_x1 = max(0, band_x2 - text_w - 2 * pad)
            band_y1 = max(0, y1)
            band_y2 = min(h, band_y1 + band_h)
            cv2.rectangle(
                overlay, (band_x1, band_y1), (band_x2, band_y2), (0, 255, 0), -1
            )
            cv2.putText(
                overlay,
                label,
                (band_x1 + pad, band_y1 + pad + text_h),
                font,
                font_scale,
                (0, 0, 0),
                font_thickness,
                cv2.LINE_AA,
            )
        return overlay

    @staticmethod
    def _write_detections_sidecar(capture_folder, capture_id, detection_data):
        """Best-effort atomic write of ``{capture_folder}/{capture_id}.
        detections.json`` carrying ``detection_data`` (the exact
        ``{"detections": {...}}`` payload the capture-record block encodes).

        Written temp-then-rename in the destination directory so readers
        never observe a partial file. ANY failure (missing/unwritable
        folder, serialization error) is logged and swallowed — the sidecar
        must never fail inference (workflow-engine Requirement 1.8
        containment posture)."""
        try:
            if not capture_folder or not capture_id:
                return
            final_path = os.path.join(
                capture_folder, "{0}.detections.json".format(capture_id)
            )
            tmp_path = "{0}.tmp".format(final_path)
            with open(tmp_path, "w") as sidecar_file:
                json.dump(detection_data, sidecar_file)
            os.rename(tmp_path, final_path)
        except Exception:  # noqa: BLE001 - best-effort, never fail inference
            log.warning(
                "Could not write detections sidecar for capture %s in %s",
                capture_id,
                capture_folder,
                exc_info=True,
            )

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
        # A detection capture is recognized solely by the presence of a
        # 'bounding_box' field in the reused anomalies payload (incl. the
        # zero-object sentinel). Detection captures are typed distinctly and
        # must never be labeled "Anomaly"/"Normal".
        is_detection = self._is_detection_list(inference_anomalies)
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
        if is_detection:
            # Detection form: keep the numeric anomaly-label-detected flag and
            # existing keys intact for downstream consumers, but use a
            # detection-appropriate class-name distinct from the anomaly wording.
            class_name = "Detection"
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
        # Overlay reference relocation: previously the overlay data-ref was
        # emitted ONLY when an anomaly mask was present, so detection captures
        # (which carry an empty mask) never received one. Emit it whenever an
        # overlay is present, i.e. for any detection capture (incl. the
        # zero-object sentinel case, whose overlay is an unannotated source
        # copy) OR any anomaly capture that has a mask. The anomaly-mask case
        # continues to emit the ref exactly as before.
        #
        # Division of responsibility: this method only decides whether to
        # EMIT the overlay data-ref; it does not produce/encode the overlay
        # bytes. The overlay JPEG is written by the gstreamer capture plugin
        # from the `overlay` tensor produced in execute(), so encode success
        # is not observable here. The guarantee that detection captures always
        # produce a non-empty overlay tensor is handled in execute() (task 6.1),
        # and best-effort omission on encode failure belongs where the bytes
        # are produced, not here.
        overlay_present = is_detection or self._has_anomaly_mask(inference_mask, input_image)
        if overlay_present:
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
        if is_detection:
            # Detection captures set output=1 but must be typed distinctly and
            # never labeled "Anomaly"/"Normal".
            inf_result["Inference result"] = "Detection"
        elif inference_output:
            inf_result["Inference result"] = "Anomaly"
        else:
            inf_result["Inference result"] = "Normal"
        if is_detection:
            # Count only VALID detections: entries whose bounding_box is a
            # list of length 4. The zero-object sentinel carries
            # bounding_box == [] and therefore counts as 0.
            valid_confidences = [
                float(det.get("confidence", 0.0))
                for det in inference_anomalies
                if isinstance(det.get("bounding_box"), list)
                and len(det.get("bounding_box")) == 4
            ]
            inf_result["Detection_count"] = len(valid_confidences)
            # Derive the capture confidence robustly from the detection
            # entries: max object confidence when there is at least one valid
            # object, else 0.0 for the zero-object sentinel.
            inf_result["Confidence"] = max(valid_confidences) if valid_confidences else 0.0
        else:
            # Anomaly path is unchanged: report the model's inference confidence.
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
        # anomaly list (or detection list when task=object_detection)
        anomalies = inference_anomalies
        if self._is_detection_list(anomalies):
            # Object-detection payload: emit a detections block instead of the
            # anomaly/segmentation block, then fall through to eventMetadata.
            #
            # Emit ONLY valid detections (a 4-element bounding_box), filtering
            # out the zero-object sentinel (empty bounding_box) so the block
            # reflects only real objects, and re-index with contiguous string
            # keys "0","1",... over the surviving detections. Each entry retains
            # the original class index (as class_index) alongside a resolved
            # human-readable class_label.
            det_map = {}
            idx = 0
            for det in anomalies:
                box = det.get("bounding_box") or []
                if len(box) != 4:
                    continue
                class_index = str(det.get("class", ""))
                # Prefer the label the base model embedded in the payload; if it
                # is missing/empty, re-resolve via the shared util and finally
                # fall back to the class-index string.
                class_label = det.get("class_label") or ""
                if not class_label:
                    class_label = resolve_class_label(class_index, None)
                if not class_label:
                    class_label = class_index
                det_map[str(idx)] = {
                    "class_index": class_index,
                    "class_label": class_label,
                    "bounding_box": box,
                    "confidence": det.get("confidence", 0.0),
                }
                idx += 1
            detection_data = {"detections": det_map}
            # Detections sidecar (detection-guided-bedrock-inspection,
            # design Risk 1 fallback): the capture record only lands at
            # {output_dir}/{capture_id}.jsonl when a terminal emlcapture
            # element publishes the broker file targets — a graph WITHOUT a
            # capture node never routes them, so the workflow engine's
            # read_detections would find nothing. Persist the same
            # detections map directly from the marshal, keyed by capture
            # id, so the reader has a marshal-owned source that does not
            # depend on capture-node routing. Best-effort: a sidecar write
            # failure must never fail inference.
            self._write_detections_sidecar(
                capture_meta_data.get("capture_folder", ""),
                capture_id,
                detection_data,
            )
            detection_str = json.dumps(detection_data)
            detection_str_encoded = base64.b64encode(detection_str.encode()).decode()
            ret["deviceFleetAuxiliaryOutputs"].append(
                {
                    "data": detection_str_encoded,
                    "encoding": "BASE64",
                    "observedContentType": "json_with_base64_encoding",
                }
            )
        else:
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
            if self._is_detection_list(inference_anomalies):
                # task=object_detection: the reused anomalies payload is a list
                # of detection results. Draw boxes on the input image as the
                # overlay; emit an empty mask (detection has no pixel mask).
                detection_overlay = self._generate_detection_overlay(
                    input1.as_numpy(), inference_anomalies
                )
                encoded_overlay = self._encode_overlay(detection_overlay).astype(
                    self.output_overlay_dtype
                )
                overlay_tensor = pb_utils.Tensor("overlay", encoded_overlay)
                mask_tensor = pb_utils.Tensor("mask", np.array([]).astype(self.output_mask_dtype))
            elif self._has_anomaly_mask(inference_mask.as_numpy(), input1.as_numpy()):
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
