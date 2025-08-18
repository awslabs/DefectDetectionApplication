# NOTE: This file is related to lfv_model_template.py, keep them in sync.
import numpy as np
import time
import os
import logging
import json
import typing
import ctypes

# triton_python_backend_utils is available in every Triton Python model. You
# need to use this module to create inference requests and responses. It also
# contains some utility functions for extracting information from model_config
# and converting Triton input/output types to numpy types.
import triton_python_backend_utils as pb_utils

from lyra_anomalies_mask_utils import (
    DEFAULT_ANOMALY_MASK_PALETTE,
    convert_index_mask_to_color_mask,
    get_classes_areas,
    hex_color_string,
)

from lyra_science_processing_utils.model_config import ModelConfig
from lyra_science_processing_utils.model_graph_factory import ModelGraphFactory
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult
from lyra_science_processing_utils.utils.inference_data import InferenceData

log = logging.getLogger(__name__)
DLR_DEVICE_TYPE_MAP = {
    1: "cpu",
    2: "gpu",
    4: "opencl",
}
"""
MockDLR 
Keep everything from LFV staging, but pass it a mock runtime instead of a real DLR Model object
Use DLR meta file `compiled.meta` to get output shapes
Generate random output based on valid output shapes
"""


class MockDLR(object):
    def __init__(self, model_path: str, device_type: str = "cpu", device_id: int = 0):
        self.model_path = model_path
        self.device_type = device_type
        self.device_id = device_id
        # read DLR compiled meta to get output_shape
        compiled_meta_path = os.path.join(model_path, "compiled.meta")
        assert os.path.exists(compiled_meta_path)
        self.meta_json = None
        try:
            with open(compiled_meta_path, "r") as f:
                self.meta_json = json.loads(f.read())
        except OSError as e:
            log.error(f"Failed to read compiled meta file: {e.strerror}")
        except Exception as e:
            log.error(f"Failed to read compiled meta file: {e}")

    def run(self, input_data: np.ndarray):
        outputs_meta = self.meta_json["Model"].get("Outputs", [])
        outputs = []
        # for every output get random values of valid output shape
        for out in outputs_meta:
            shape = out.get("shape", [])
            outputs.append(np.random.randn(*shape).astype(out["dtype"]))
        return outputs


def dlr_device_type(model_path):
    return DLR_DEVICE_TYPE_MAP[1]


class _InferenceRunner:  # pragma: no cover
    """
    Callable class. Implements DLR inference.
    """

    def __init__(
        self,
        model_id: str,
        model_path: str,
        device_id: int = 0,
    ):
        """
        :model_path: Location of the compiled DLR model on the disk.
        :device_type: Device where inference should be executed. "cpu" or "gpu". "gpu" is default.
        :device_id: Index of the gpu device. 0 is default.
        """
        self.__model = self.__load_model(
            model_id,
            model_path,
            device_id,
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
        return self.__model.run(inference_input)

    @staticmethod
    def __load_model(
        model_id: str,
        model_path: str,
        device_id: int = 0,
    ):
        """
        Loads compiled DLR model.

        :model_path: Location of the compiled DLR model on the disk.
        :device_type: Device where inference should be executed. "cpu" or "gpu".
        :device_id: Index of the gpu device.
        :returns: MockDLR model.
        """
        # dlr_device_type has dependence over dlr
        device_type = "cpu"

        log.info(
            f"{model_path}: Starting loading: dev_type: {device_type}, dev_id: {device_id}, model: {model_id}"
        )
        model = MockDLR(
            model_path,
            device_type,
            device_id,
        )
        log.info(f"{model_path}: Initialization complete for model {model_id}")
        return model


class TritonPythonModel:
    """Your Python model must use the same class name. Every Python model
    that is created must have "TritonPythonModel" as the class name.
    """

    MANIFEST_FILENAME = "manifest.json"
    MODEL_GRAPH_MANIFEST_KEY = "model_graph"
    DATASET_MANIFEST_KEY = "dataset"
    DATASET_IMAGE_WIDTH_MANIFEST_KEY = "image_width"
    DATASET_IMAGE_HEIGHT_MANIFEST_KEY = "image_height"

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
            inference_output = self.__model_graph.predict(input_np)
            anomaly_result: AnomalyResult = inference_output.objects[0].anomaly  # type: ignore
            is_anomalous = anomaly_result.label.lower() == "anomaly"  # type: ignore
            is_anomalous = np.uint8([is_anomalous])
            anomaly_mask = None
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

            inference_response = pb_utils.InferenceResponse(output_tensors=output_tensors)
            responses.append(inference_response)
        return responses

    def finalize(self):
        """`finalize` is called only once when the model is being unloaded.
        Implementing `finalize` function is OPTIONAL. This function allows
        the model to perform any necessary clean ups before exit.
        """
        log.info("Cleaning up...")

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
        return (
            ModelConfig(manifest[TritonPythonModel.MODEL_GRAPH_MANIFEST_KEY]),
            dataset_image_dimensions,
        )
