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

# NOTE: This file is related to mock_model_template.py, keep them in sync.
import numpy as np
import time
import os
import logging
import json
import typing

# triton_python_backend_utils is available in every Triton Python model. You
# need to use this module to create inference requests and responses. It also
# contains some utility functions for extracting information from model_config
# and converting Triton input/output types to numpy types.
import triton_python_backend_utils as pb_utils

# The DDA app packages (lyra_anomalies_mask_utils / lyra_science_processing_utils)
# are installed at the container app root (default "/"). The Triton Python-backend
# stub does NOT forward the parent process's PYTHONPATH and is launched with a
# working directory that is not the app root, so those packages are otherwise
# unresolvable here (ModuleNotFoundError). Ensure the app root is on sys.path
# regardless of the stub's CWD/environment before importing them.
import sys

_DDA_APP_ROOT = os.environ.get("DDA_APP_ROOT", "/")
if _DDA_APP_ROOT and _DDA_APP_ROOT not in sys.path:
    sys.path.insert(0, _DDA_APP_ROOT)

# The pluggable runtime module (inference_runtimes.py) is copied by
# model_convertor.py into THIS model's version directory, next to this file.
# The Triton Python-backend stub does not put that directory on sys.path (and
# its CWD is elsewhere), so a bare `import inference_runtimes` raises
# ModuleNotFoundError. Add this file's own directory to sys.path so the sibling
# module resolves regardless of the stub's environment.
_MODEL_DIR = os.path.dirname(os.path.abspath(__file__))
if _MODEL_DIR and _MODEL_DIR not in sys.path:
    sys.path.insert(0, _MODEL_DIR)

from lyra_anomalies_mask_utils import (
    DEFAULT_ANOMALY_MASK_PALETTE,
    convert_index_mask_to_color_mask,
    get_classes_areas,
    hex_color_string,
)

from lyra_science_processing_utils.model_config import ModelConfig
from lyra_science_processing_utils.model_graph_factory import ModelGraphFactory
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult
from lyra_science_processing_utils.utils.class_label_map import resolve_class_label
from lyra_science_processing_utils.utils.inference_data import InferenceData

log = logging.getLogger(__name__)


class _InferenceRunner:  # pragma: no cover
    """
    Callable class. Delegates to a pluggable inference engine selected by the
    model package manifest's ``runtime`` field (``dlr`` default | ``onnx`` |
    ``pytorch``). See docs/multi-runtime-inference.md.

    The DLR path is the default and is behaviorally unchanged: when ``runtime``
    is absent or ``"dlr"`` this loads the model exactly as before. The actual
    engine implementations live in ``inference_runtimes.py`` (copied next to
    this file by model_convertor.py).
    """

    def __init__(
        self,
        model_id: str,
        model_path: str,
        device_id: int = 0,
        runtime: str = "dlr",
        runtime_artifact: typing.Optional[str] = None,
        device: typing.Optional[str] = None,
    ):
        """
        :model_id: Unique model+version id, for logging.
        :model_path: Stage directory holding the engine artifact on disk.
        :device_id: Index of the gpu device. 0 is default.
        :runtime: Engine identifier from the manifest. Default "dlr".
        :runtime_artifact: Engine artifact filename within the stage dir.
        :device: Optional "cpu"/"gpu" override; default auto-detect.
        """
        self.__model = self.__load_model(
            model_id,
            model_path,
            device_id,
            runtime,
            runtime_artifact,
            device,
        )

    def __call__(
        self,
        inference_input: np.ndarray,
    ) -> typing.List[np.array]:
        """
        Runs model inference.

        :inference_input: Input tensor.
        :return: Output tensor.
        """
        return self.__model(inference_input)

    @staticmethod
    def __load_model(
        model_id: str,
        model_path: str,
        device_id: int = 0,
        runtime: str = "dlr",
        runtime_artifact: typing.Optional[str] = None,
        device: typing.Optional[str] = None,
    ):
        """
        Builds the inference engine via the runtime factory.

        :returns: A callable runner honoring runner(input_np) -> list[np.ndarray].
        """
        from inference_runtimes import make_runner

        return make_runner(
            runtime,
            model_id=model_id,
            model_dir=model_path,
            device_id=device_id,
            artifact=runtime_artifact,
            device=device,
        )


class TritonPythonModel:
    """Your Python model must use the same class name. Every Python model
    that is created must have "TritonPythonModel" as the class name.
    """

    MANIFEST_FILENAME = "manifest.json"
    MODEL_GRAPH_MANIFEST_KEY = "model_graph"
    DATASET_MANIFEST_KEY = "dataset"
    DATASET_IMAGE_WIDTH_MANIFEST_KEY = "image_width"
    DATASET_IMAGE_HEIGHT_MANIFEST_KEY = "image_height"
    RUNTIME_MANIFEST_KEY = "runtime"
    RUNTIME_ARTIFACT_MANIFEST_KEY = "runtime_artifact"
    RUNTIME_DEVICE_MANIFEST_KEY = "device"
    TASK_MANIFEST_KEY = "task"
    TASK_ANOMALY = "anomaly"
    TASK_OBJECT_DETECTION = "object_detection"
    CLASS_NAMES_MANIFEST_KEY = "class_names"
    DETECTION_MANIFEST_KEY = "detection"

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
        """
        # Warm up load model.
        for i in range(3):
            inp = (np.random.rand(dims[2], dims[3],3) * 255.0).astype(np.float32)
            out = self.dlr_model.run(inp)
        """
        self.model_config = model_config = json.loads(args["model_config"])
        self.models_dir = os.path.dirname(os.path.abspath(__file__))
        # concat model name and version
        self.__model_id = "{}_{}".format(args["model_name"], args["model_version"])
        log.info(f"Model loading started for model {self.__model_id}.")
        (
            self.__model_graph_config,
            self.__model_dataset_images_dimensions,
        ) = self.__load_model_graph_config(self.models_dir)

        self.__model_supports_anomaly_localization = bool(
            len(self.__model_graph_config.get_pixel_level_classes())
        )

        self.__anomaly_threshold = self.__model_graph_config.get_threshold()

        # Read the optional pluggable-runtime config from the manifest. Absent
        # => DLR (full backward compatibility). See multi-runtime design doc.
        runtime, runtime_artifact, runtime_device = self.__load_runtime_config(self.models_dir)
        log.info(f"Model {self.__model_id} using inference runtime '{runtime}'.")

        # Read the optional task selector. Absent => anomaly (full backward
        # compat). object_detection routes execute() through the bbox emit path.
        self.__task = self.__load_task(self.models_dir)
        log.info(f"Model {self.__model_id} task '{self.__task}'.")

        # Read the optional class-name map from the manifest once, so detection
        # labels can be resolved in __build_detection_tensors. Absent => None
        # (the shared resolver falls back to the default COCO map / index string).
        self.__class_names = self.__load_class_names(self.models_dir)

        inference_runners = []
        for idx in range(self.__model_graph_config.num_stages()):
            stage_type = self.__model_graph_config.get_stage_type(idx)
            inference_runners.append(
                _InferenceRunner(
                    self.__model_id,
                    os.path.join(
                        self.models_dir,
                        stage_type,
                    ),
                    runtime=runtime,
                    runtime_artifact=runtime_artifact,
                    device=runtime_device,
                )
            )

        self.__model_graph = ModelGraphFactory.get_model_graph(
            self.__model_graph_config,
            inference_runners,  # type: ignore
        )
        log.info(f"Model loading completed for model {self.__model_id}.")
        # Check if there are pixel level classes, for anomaly localization purposes.
        self.__model_supports_anomaly_localization = bool(
            len(self.__model_graph_config.get_pixel_level_classes())
        )
        # Warm up is complete by model graph by this point.
        input_config = pb_utils.get_input_config_by_name(model_config, "input")
        self.input_dtype = pb_utils.triton_string_to_numpy(input_config["data_type"])
        output0_config = pb_utils.get_output_config_by_name(model_config, "output")
        self.output_dtype = pb_utils.triton_string_to_numpy(output0_config["data_type"])
        output1_config = pb_utils.get_output_config_by_name(model_config, "mask")
        self.mask_dtype = pb_utils.triton_string_to_numpy(output1_config["data_type"])
        score_config = pb_utils.get_output_config_by_name(model_config, "output_score")
        self.score_dtype = pb_utils.triton_string_to_numpy(score_config["data_type"])
        confidence_config = pb_utils.get_output_config_by_name(model_config, "output_confidence")
        self.confidence_dtype = pb_utils.triton_string_to_numpy(confidence_config["data_type"])
        anomalies_config = pb_utils.get_output_config_by_name(model_config, "anomalies")
        self.anomalies_dtype = pb_utils.triton_string_to_numpy(anomalies_config["data_type"])

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
            in_0 = pb_utils.get_input_tensor_by_name(request, "input")
            input_np = in_0.as_numpy()
            # Diagnostic: the exact image tensor the base model receives from
            # Triton/emltriton. A shape other than (H, W, 3) or a value range
            # other than 0..255 would corrupt the downstream preprocess/inference
            # (e.g. yielding no detections) and explains pipeline-vs-standalone
            # divergence.
            try:
                log.info(
                    "base model input tensor: shape=%s dtype=%s min=%s max=%s task=%s",
                    getattr(input_np, "shape", None),
                    getattr(input_np, "dtype", None),
                    float(input_np.min()) if input_np.size else None,
                    float(input_np.max()) if input_np.size else None,
                    self.__task,
                )
            except Exception:
                pass
            inference_output = self.__model_graph.predict(input_np)

            if self.__task == TritonPythonModel.TASK_OBJECT_DETECTION:
                output_tensors = self.__build_detection_tensors(inference_output, input_np)
            else:
                output_tensors = self.__build_anomaly_tensors(inference_output, input_np)

            inference_response = pb_utils.InferenceResponse(output_tensors=output_tensors)
            responses.append(inference_response)
        return responses

    def __build_detection_tensors(self, inference_output, input_np):
        """Emit object-detection results through the existing output contract.

        To avoid a Triton rebuild or ensemble rewiring, detections ride through
        the existing variable-length ``anomalies`` tensor as a serialized JSON
        list of ObjectDetectionResults; ``output`` is set to 1 when any
        detection is present and ``output_score``/``output_confidence`` carry the
        top detection's confidence. ``mask`` is emitted empty.
        """
        # Collect ObjectDetectionResults from the per-object inference data.
        detections = []
        for obj in getattr(inference_output, "objects", []) or []:
            det = getattr(obj, "object_detection", None)
            if det is not None:
                detections.append(det)

        # Serialize each detection and embed a human-readable class_label
        # (resolved from the manifest class-name map, falling back to the
        # default COCO map / index string) alongside the retained numeric
        # `class` index. The tensor set/signature is unchanged.
        serialized = []
        for d in detections:
            entry = d.serialize()
            entry["class_label"] = resolve_class_label(d.obj_class, self.__class_names)
            serialized.append(entry)
        top_conf = max((float(d.confidence) for d in detections), default=0.0)
        has_detection = np.uint8([1 if detections else 0])
        confidence = np.float32([top_conf])
        score = np.float32([top_conf])

        # When there are no real detections, emit a single zero-object sentinel
        # entry (carrying an empty `bounding_box`) instead of `[]`. This keeps a
        # zero-object detection recognizable to the Marshal's `_is_detection_list`
        # (which requires a non-empty list whose first entry has a `bounding_box`
        # key), since an empty `[]` is byte-identical to the anomaly "no anomalies"
        # payload and would otherwise be misrouted. The sentinel carries no
        # drawable box. `output`/`output_confidence`/`output_score` are unchanged
        # (has_detection stays 0, top_conf stays 0.0); only the JSON content of the
        # `anomalies` tensor changes for the empty case.
        payload = serialized if serialized else [
            {
                "bounding_box": [],
                "class": "",
                "class_label": "",
                "confidence": 0.0,
                "no_objects": True,
            }
        ]

        detections_bytes = np.frombuffer(
            bytes(json.dumps(payload), encoding="utf-8"), dtype=np.uint8
        )
        empty_mask = np.zeros(input_np.shape)

        return [
            pb_utils.Tensor("output", has_detection.astype(self.output_dtype)),
            pb_utils.Tensor("output_confidence", confidence.astype(self.confidence_dtype)),
            pb_utils.Tensor("output_score", score.astype(self.score_dtype)),
            pb_utils.Tensor("mask", empty_mask.astype(self.mask_dtype)),
            pb_utils.Tensor("anomalies", detections_bytes.astype(self.anomalies_dtype)),
        ]

    def __build_anomaly_tensors(self, inference_output, input_np):
        """Emit anomaly-classification results (the original output contract)."""
        anomaly_result: AnomalyResult = inference_output.objects[0].anomaly  # type: ignore
        is_anomalous = anomaly_result.label.lower() == "anomaly"  # type: ignore
        is_anomalous = np.uint8([is_anomalous])
        anomalies = None
        confidence = np.float32([anomaly_result.confidence])
        score = np.float32([anomaly_result.score])
        output_tensors = []
        out_tensor_1 = pb_utils.Tensor("output", is_anomalous.astype(self.output_dtype))
        out_tensor_3 = pb_utils.Tensor(
            "output_confidence", confidence.astype(self.confidence_dtype)
        )
        out_tensor_4 = pb_utils.Tensor("output_score", score.astype(self.score_dtype))
        output_tensors.append(out_tensor_1)
        output_tensors.append(out_tensor_3)
        output_tensors.append(out_tensor_4)

        if anomaly_result.mask is not None and self.__model_supports_anomaly_localization:
            # Outputting anomaly mask only if it was generated by the model and configuration contains pixel level classes.
            rgb_mask = convert_index_mask_to_color_mask(anomaly_result.mask)
            pixel_classes_names = self.__model_graph_config.get_pixel_level_classes()
            pixel_classes_areas = get_classes_areas(anomaly_result.mask)
            anomalies = [
                {
                    "name": pixel_classes_names[class_index],
                    "total_percentage_area": class_area,
                    "hex_color": hex_color_string(
                        DEFAULT_ANOMALY_MASK_PALETTE[class_index].tolist(),
                    ),
                }
                for class_index, class_area in pixel_classes_areas
            ]
            anomalies = np.frombuffer(
                bytes(json.dumps(anomalies), encoding="utf-8"), dtype=np.uint8
            )
            out_tensor_2 = pb_utils.Tensor("mask", rgb_mask.astype(self.mask_dtype))
            out_tensor_5 = pb_utils.Tensor("anomalies", anomalies.astype(self.anomalies_dtype))
            output_tensors.append(out_tensor_2)
            output_tensors.append(out_tensor_5)
        else:
            temp = np.zeros(input_np.shape)
            out_tensor_2 = pb_utils.Tensor("mask", temp.astype(self.mask_dtype))
            anomalies = np.frombuffer(bytes(json.dumps([]), encoding="utf-8"), dtype=np.uint8)
            out_tensor_5 = pb_utils.Tensor("anomalies", anomalies.astype(self.anomalies_dtype))
            output_tensors.append(out_tensor_2)
            output_tensors.append(out_tensor_5)
        return output_tensors

    def finalize(self):
        """`finalize` is called only once when the model is being unloaded.
        Implementing `finalize` function is OPTIONAL. This function allows
        the model to perform any necessary clean ups before exit.
        """
        log.info("Cleaning up...")

    def __load_runtime_config(
        self,
        model_dir: str,
    ) -> typing.Tuple[str, typing.Optional[str], typing.Optional[str]]:
        """
        Read the optional pluggable-runtime selector from the manifest.

        :param model_dir: Directory with the unpacked model (holds manifest.json).
        :returns: (runtime, runtime_artifact, device). Defaults to
            ("dlr", None, None) when the fields are absent, preserving full
            backward compatibility for existing DLR model packages.
        """
        manifest_path = os.path.join(model_dir, TritonPythonModel.MANIFEST_FILENAME)
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError) as e:
            log.warning(f"Could not read runtime config from manifest: {e}; defaulting to dlr")
            return ("dlr", None, None)
        runtime = manifest.get(TritonPythonModel.RUNTIME_MANIFEST_KEY, "dlr") or "dlr"
        runtime_artifact = manifest.get(TritonPythonModel.RUNTIME_ARTIFACT_MANIFEST_KEY)
        device = manifest.get(TritonPythonModel.RUNTIME_DEVICE_MANIFEST_KEY)
        return (str(runtime).lower(), runtime_artifact, device)

    def __load_task(self, model_dir: str) -> str:
        """
        Read the optional task selector from the manifest.

        :returns: "anomaly" (default) or "object_detection". Defaults to anomaly
            when absent/unreadable, preserving backward compatibility.
        """
        manifest_path = os.path.join(model_dir, TritonPythonModel.MANIFEST_FILENAME)
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError) as e:
            log.warning(f"Could not read task from manifest: {e}; defaulting to anomaly")
            return TritonPythonModel.TASK_ANOMALY
        task = manifest.get(TritonPythonModel.TASK_MANIFEST_KEY, TritonPythonModel.TASK_ANOMALY)
        return str(task).lower() if task else TritonPythonModel.TASK_ANOMALY

    def __load_class_names(self, model_dir: str) -> typing.Optional[dict]:
        """
        Read the optional class-name map from the manifest.

        :param model_dir: Directory with the unpacked model (holds manifest.json).
        :returns: The manifest ``class_names`` mapping if present, else the
            ``dataset.class_names`` mapping if present, else ``None``. On an
            unreadable/invalid manifest, logs a warning and returns ``None``,
            preserving backward compatibility (labels fall back to COCO / index).
        """
        manifest_path = os.path.join(model_dir, TritonPythonModel.MANIFEST_FILENAME)
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError) as e:
            log.warning(f"Could not read class names from manifest: {e}; defaulting to None")
            return None
        class_names = manifest.get(TritonPythonModel.CLASS_NAMES_MANIFEST_KEY)
        if class_names is None:
            dataset_details = manifest.get(TritonPythonModel.DATASET_MANIFEST_KEY) or {}
            class_names = dataset_details.get(TritonPythonModel.CLASS_NAMES_MANIFEST_KEY)
        return class_names

    def __load_model_graph_config(
        self,
        model_dir: str,
    ) -> typing.Tuple[ModelConfig, typing.Optional[typing.Tuple[int, int]]]:
        """
        Method loads model graph config.
        :param model_dir: Directory with unpacked model.
        :returns: Tuple of ModelConfig and image size used for training the model.
        """
        log.debug(f"Loading model configuration from {model_dir}")
        manifest_path = os.path.join(
            model_dir,
            TritonPythonModel.MANIFEST_FILENAME,
        )
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)

        dataset_image_dimensions = None
        if TritonPythonModel.DATASET_MANIFEST_KEY in manifest:
            dataset_details = manifest[TritonPythonModel.DATASET_MANIFEST_KEY]
            if (
                TritonPythonModel.DATASET_IMAGE_WIDTH_MANIFEST_KEY in dataset_details
                and TritonPythonModel.DATASET_IMAGE_HEIGHT_MANIFEST_KEY in dataset_details
            ):
                dataset_image_dimensions = (
                    dataset_details[TritonPythonModel.DATASET_IMAGE_WIDTH_MANIFEST_KEY],
                    dataset_details[TritonPythonModel.DATASET_IMAGE_HEIGHT_MANIFEST_KEY],
                )
        log.debug(f"loaded model dataset_details = {dataset_image_dimensions}")
        model_graph = manifest[TritonPythonModel.MODEL_GRAPH_MANIFEST_KEY]
        # The converter writes the detection decode config (score_threshold,
        # iou_threshold, class_names, network_input, ...) as a TOP-LEVEL
        # manifest block, but ModelGraphFactory constructs each stage's
        # postprocessor from the per-stage dict only. Merge the block into the
        # stages so detection postprocessors see their configured thresholds
        # instead of silently falling back to their defaults.
        detection_config = manifest.get(TritonPythonModel.DETECTION_MANIFEST_KEY)
        if isinstance(detection_config, dict):
            for stage in model_graph.get("stages", []):
                stage.setdefault(
                    TritonPythonModel.DETECTION_MANIFEST_KEY, detection_config
                )
        return (
            ModelConfig(model_graph),
            dataset_image_dimensions,
        )
