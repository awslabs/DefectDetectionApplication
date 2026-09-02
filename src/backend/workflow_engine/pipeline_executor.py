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

"""WorkflowExecutor: runs triggered workflow pipelines (Requirements 9.2, 9.3, 9.7).

Registered through :func:`workflow_engine.executor.set_executor`, so every
run happens on the dedicated daemon thread the hook spawns — never on the
API thread and never on the Pipeline_Configuration path (Requirements
13.4, 13.7). Per run the executor:

1. Loads the WorkflowExecution + WorkflowRegistration rows and the
   registration's ``compiled_pipeline.json``.
2. Renders the launch string (``workflow_engine.rendering`` — the same
   dialect the existing builder produces) and the element-name -> nodeId
   map.
3. Scopes the component's ``plugins/<arch>/`` directory to the run
   (``workflow_engine.gst_plugins``) and executes the string through a
   **fresh** ``GstPipelineManager`` instance — inheriting its watchdog,
   error capture, and emltriton tag parsing, so model inference flows
   through emltriton -> embedded Triton exactly as Pipeline_Configuration
   runs do (Requirements 9.2, 9.3, 13.8). The shared manager instance
   used by ``gst_pipeline_executor`` for Pipeline_Configurations is never
   touched (Requirements 13.1, 13.4).
4. On failure, maps the failing element back to its workflow node via the
   compiled-document tags and records status ``failed`` with
   ``failing_node_id``/``error`` on the execution row — the record the
   ``/workflows/executions/{id}`` status endpoint surfaces
   (Requirement 9.7).
5. On success, records ``completed`` and hands the parsed tag values plus
   the document's ``executorBindings`` to the post-run handler — the hook
   task 12.4 (output bindings) plugs into.

Camera_Binding resolution and the Aravis frame feed (aravis-camera-input
Requirements 6.4, 6.5, 6.6): an injectable ``binding_resolution_provider``
(wired at engine startup to the watcher's ``binding_resolution()``
accessor) supplies the registration's resolved Camera_Bindings. When a
resolution exists the executor runs its slot-substituted document;
provider absence or failure falls back to the on-disk document, logged,
never failing a run for documents that don't need bindings. Before the
pipeline starts, ``plan_aravis_feeds`` plans the document's Aravis frame
feed; the executor grabs one frame through the camera manager and pushes
it into the compiled appsrc through the existing
``run_pipeline(launch_string, frame_data)`` Frame_Feed — the classic
Camera-type execution model. Planning and grab failures fail the run
with ``failing_node_id`` set to the Aravis node; documents with no
Aravis binding points take the exact pre-feature call path.

The Custom Python source feed (custom-python-source Requirements 7.1-7.6)
rides the same Frame_Feed model: ``plan_python_sources`` plans the
document's Frame_Producer from its ``pythonSourceBinding`` point, the
executor runs ``produce_frame(context)`` in a producer bridge subprocess
before the pipeline starts, and the Produced_Frame is pushed through the
compiled appsrc with its explicit caps — alone via
``run_pipeline(launch_string, frame_data)`` or alongside pumped
emlpython bridges via ``run_bridged_pipeline(..., frame_data=...)``.
Producer metadata merges into the Run_Metadata under
``python_source.<nodeId>`` before the trigger seeding.

Any exception anywhere in a run is contained: the execution row is marked
failed and nothing propagates (Requirement 13.7).
"""

import copy
import inspect
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from workflow_engine import branching, csi_capture, detections, run_artifacts
from workflow_engine import executor as executor_hook
from workflow_engine import python_bridge, rendering
from workflow_engine.aravis_feed import AravisFeedError, plan_aravis_feeds
from workflow_engine.python_source import (
    PythonSourceError,
    plan_python_sources,
)
from workflow_engine.output_bindings import (
    BINDING_BEDROCK_INFERENCE,
    BINDING_LLM_INFERENCE,
    BedrockInferenceProcessor,
    LlmInferenceProcessor,
    OutputBindingError,
    RunContext,
)
from workflow_engine.discovery import (
    COMPILED_PIPELINE_FILE,
    MANIFEST_FILE,
    STATUS_REGISTERED,
    WORKFLOW_FILE,
)
from workflow_engine import gst_plugins
from workflow_engine.gst_plugins import workflow_plugin_path
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.node_status import NodeStatusCollector, format_duration_ms
from workflow_engine.run_log import RunLogCapture

logger = logging.getLogger(__name__)

#: Default Triton model repository. Enumerated from the FILESYSTEM (never via
#: the Triton client) so model-name resolution can never stall on server state.
_TRITON_MODEL_REPO = "/aws_dda/dda_triton/triton_model_repo"


def _loaded_ensemble_models(repo: str = _TRITON_MODEL_REPO) -> list:
    """The deployed Triton ensemble model names present in ``repo``.

    Only the top-level ``model-*`` ensembles are returned; the
    ``base_model-*`` / ``marshal_*`` sub-models Triton composes them from
    are excluded (they are never referenced by a workflow). A missing or
    unreadable repo yields ``[]`` (resolution then no-ops)."""
    try:
        entries = sorted(os.listdir(repo))
    except OSError:
        return []
    return [
        e for e in entries
        if e.startswith("model-")
        and not e.startswith(("base_model-", "marshal_model-"))
        and os.path.isdir(os.path.join(repo, e))
    ]


#: Matches a Triton ``config.pbtxt`` ``input { ... name: "METADATA" ... }``
#: block — the marshal-metadata contract the DDA ensemble models declare.
#: pbtxt input blocks don't nest, so a non-brace run inside the block is a
#: safe, dependency-free way to require METADATA be an *input* (not an
#: identically named output).
_METADATA_INPUT_PATTERN = re.compile(
    r'input\s*\{[^{}]*name:\s*"METADATA"', re.DOTALL
)

#: Matches a Triton ``config.pbtxt`` ``output { ... name: "<name>" ... }``
#: block, capturing the declared output tensor name. Same non-nesting
#: assumption as :data:`_METADATA_INPUT_PATTERN` (pbtxt ``output`` blocks
#: don't nest), so this enumerates the ensemble's declared outputs
#: (``output_overlay``, ``output_mask``, ``output_capture``,
#: ``output_anomalous``, ``output_confidence``, ...) without a pbtxt parser.
_OUTPUT_NAME_PATTERN = re.compile(
    r'output\s*\{[^{}]*name:\s*"([^"]+)"', re.DOTALL
)


def _model_declares_metadata_input(
    model_name: str, repo: Optional[str] = None
) -> bool:
    """True when the deployed Triton model ``model_name`` declares a
    ``METADATA`` input in its ``config.pbtxt``.

    The DDA anomaly/segmentation ensembles route a JSON ``METADATA`` tensor
    to their marshal step; plain models don't declare it. Reading the
    on-disk config (never the Triton client) lets the executor inject the
    metadata only for models that actually require it — models without the
    input, and the fixture repos in tests, are left untouched. A missing or
    unreadable config yields False (no injection)."""
    if not model_name:
        return False
    if repo is None:
        repo = _TRITON_MODEL_REPO
    path = os.path.join(repo, model_name, "config.pbtxt")
    try:
        with open(path, "r") as config_file:
            text = config_file.read()
    except OSError:
        return False
    return bool(_METADATA_INPUT_PATTERN.search(text))


def _model_declared_outputs(
    model_name: str, repo: Optional[str] = None
) -> set:
    """The set of ``output {}`` tensor names the deployed Triton model
    ``model_name`` declares in its ``config.pbtxt``.

    The DDA ensembles declare the capture outputs their post-processing
    produces (e.g. ``output_overlay``, ``output_mask``, ``output_capture``,
    ``output_anomalous``, ``output_confidence``); :meth:`WorkflowExecutor.
    _route_capture_outputs` only routes the ``triton_inference_output_*``
    targets whose matching output the model actually declares, so plain
    models (and the fixture repos in tests) route nothing. Reads the
    on-disk config (never the Triton client); a missing or unreadable
    config yields an empty set."""
    if not model_name:
        return set()
    if repo is None:
        repo = _TRITON_MODEL_REPO
    path = os.path.join(repo, model_name, "config.pbtxt")
    try:
        with open(path, "r") as config_file:
            text = config_file.read()
    except OSError:
        return set()
    return set(_OUTPUT_NAME_PATTERN.findall(text))


def resolve_triton_model_name(model_name: str, loaded: list) -> str:
    """Map a workflow's use-case registry model name to the Triton model
    actually deployed on this device.

    The portal workflow designer stores the registry name (e.g.
    ``cookies-binary``) and the compiler bakes it verbatim into
    ``emltriton model=``, but the model is loaded into Triton under its
    architecture-specific component name (e.g.
    ``model-cookies-binary-jetson-xavier-jp6``). Match by the DDA model
    component convention ``model-{registry}-{target}`` — tolerating the
    registry's ``_`` -> ``-`` normalization in component names — and only
    rewrite on an UNAMBIGUOUS single match. An already-correct name, no
    match, or an ambiguous match is left unchanged (best effort: never
    silently pick the wrong model)."""
    if not model_name or model_name in loaded:
        return model_name
    hyphenated = model_name.replace("_", "-")
    prefix = "model-{0}".format(hyphenated)
    candidates = [m for m in loaded if m == prefix or m.startswith(prefix + "-")]
    if len(candidates) == 1:
        return candidates[0]
    return model_name


#: Image extensions the folder frame-source resolver accepts, matching the
#: Pipeline_Configuration FOLDER source (``get_oldest_image_file_path``).
_FOLDER_SOURCE_EXTENSIONS = (".jpg", ".jpeg")

#: Root the synthesized inference METADATA points its capture-data disk path
#: at; the per-workflow subdirectory basename is what the DDA ensemble's
#: marshal model derives the workflow id from. Mirrors the on-device
#: capture location (the emlcapture ``file-target_/aws_dda/captures`` sink).
_WORKFLOW_CAPTURE_ROOT = "/aws_dda/captures"

#: The ``emlcapture`` ``meta`` routing targets, keyed by the deployed
#: model's declared ``config.pbtxt`` output name. Mirrors the on-device
#: builder (``gstreamer.pipeline_builder._add_post_processing_plugins``):
#: ``{p}`` is the ``{output_dir}/{capture_id}`` path prefix, and the
#: message-broker ``file-target_`` convention yields ``{p}-{ext}`` files
#: (overlay/mask/jsonl) while the tag targets append ``_is-anomalous`` /
#: ``_confidence``. Only the entries whose output the model declares are
#: emitted, so overlay-less/plain models route only their applicable
#: targets (Requirements 1.1, 1.3, 1.5).
_CAPTURE_OUTPUT_TARGETS = {
    "output_overlay":
        "triton_inference_output_overlay:file-target_{p}-overlay.jpg",
    "output_mask":
        "triton_inference_output_mask:file-target_{p}-mask.png",
    "output_capture":
        "triton_inference_output_capture:file-target_{p}-jsonl",
    "output_anomalous":
        "triton_inference_output_anomalous:{p}_is-anomalous",
    "output_confidence":
        "triton_inference_output_confidence:{p}_confidence",
}

#: Field order the on-device builder writes the routing string in; kept so
#: the deployed ``meta`` matches the Pipeline_Configuration path byte for
#: byte for the same declared-output set.
_CAPTURE_OUTPUT_ORDER = (
    "output_overlay",
    "output_mask",
    "output_capture",
    "output_anomalous",
    "output_confidence",
)

#: The placeholder the portal compiler may leave in a capture node's
#: ``meta`` arg for the executor to fill (alongside a bare empty/absent
#: ``meta``).
_CAPTURE_META_PLACEHOLDER = "{capture_meta}"

#: Trailing elements that carry a src pad and therefore need a downstream
#: sink. The on-device builder always terminates the Triton capture branch
#: with ``emlcapture ! fakesink``; the portal capture output node compiles
#: to a bare trailing ``emlcapture``, leaving its src pad unlinked so the
#: pipeline fails with GST_FLOW_NOT_LINKED right after inference. The
#: executor appends the same fakesink (see ``_ensure_terminal_sink``).
_SRC_PAD_TERMINAL_FACTORIES = ("emlcapture",)


class FrameSourceError(Exception):
    """A file/folder image source could not be resolved to a readable frame
    (empty folder, missing file, or a failed PNG staging). Carries the
    compiled node id so the failing node surfaces on the execution row,
    mirroring :class:`AravisFeedError`."""

    def __init__(self, message: str, node_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.node_id = node_id
        #: The directory-resolved source image behind the failure, when
        #: known. Set by :meth:`WorkflowExecutor._stage_frame_sources` when
        #: JP6 PNG staging fails for a folder-resolved JPEG, so ``execute()``
        #: can relocate the bad image to ``failed/`` (mirroring the legacy
        #: ``_move_bad_folder_image_source``) instead of letting it wedge
        #: every subsequent run (folder-source-image-consumption Req 2.4).
        self.source_image: Optional[str] = None


class MissingPipelineElementError(Exception):
    """A compiled element factory is not registered with GStreamer, so
    ``Gst.parse_launch`` would fail with ``no element "<factory>"``.

    Raised by the pipeline-factory preflight after the run's workflow
    plugin scan, instead of letting the pre-parse syntax error surface
    unattributed: it carries the originating node id so the failing node
    is identified on the execution row and in the run-status graph
    (Requirement 9.7 discipline), and its message names the elements the
    run's plugin directories actually register — for a custom node, the
    declared ``elementChain`` factory must exactly match the element
    name its built plugin registers."""

    def __init__(self, message: str, node_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.node_id = node_id


@dataclass(frozen=True)
class FolderFrame:
    """One directory-resolved frame source recorded by
    :meth:`WorkflowExecutor._stage_frame_sources`: the original JPEG
    selected from the folder (``_oldest_image_in_folder``), the staged
    ``.dda_decoded.png`` when the JP6 ``pngdec`` chain required one (else
    None), and the source node id. Recorded ONLY for directory locations
    — single-file locations are never consumed, mirroring the legacy
    FILE-type semantics (folder-source-image-consumption Req 3.1)."""

    original: str
    staged_png: Optional[str]
    node_id: Optional[str]


def _oldest_image_in_folder(location: str) -> str:
    """The oldest ``.jpg``/``.jpeg`` file in ``location`` by mtime.

    Mirrors ``captured_images_utils.get_oldest_image_file_path`` — the file
    selection the Pipeline_Configuration builder's FOLDER source uses — so
    a deployed folder source picks the same frame the on-device workflow
    would. Raises :class:`FrameSourceError` when the folder holds no JPEG.
    """
    try:
        entries = [os.path.join(location, name) for name in os.listdir(location)]
    except OSError as e:
        raise FrameSourceError(
            "Cannot read image folder '{0}': {1}".format(location, e)
        )
    images = sorted(
        (
            p
            for p in entries
            if p.lower().endswith(_FOLDER_SOURCE_EXTENSIONS) and os.path.isfile(p)
        ),
        key=os.path.getmtime,
    )
    if not images:
        raise FrameSourceError(
            "No .jpg/.jpeg image files found in folder '{0}'".format(location)
        )
    return images[0]


def _stage_decoded_png(file_path: str) -> str:
    """Decode a JPEG to a sibling ``.dda_decoded.png`` with Pillow, baking
    in EXIF orientation.

    This is the JetPack 6 frame-source staging the Pipeline_Configuration
    builder performs in ``_add_file_image_source``: the model's bundled
    ``libdlr.so`` interposes its own libjpeg, so GStreamer's libjpeg-based
    ``jpegdec`` corrupts once a model is loaded. Pillow (its own
    libjpeg-turbo) decodes correctly and the compiled ``pngdec`` chain then
    reads libpng. Raises :class:`FrameSourceError` on decode/write failure
    so the run fails with a clear cause rather than a cryptic pngdec error.
    """
    try:
        from PIL import Image, ImageOps
    except Exception as e:  # noqa: BLE001 - Pillow ships in the JP6 image
        raise FrameSourceError(
            "Pillow is required to stage a PNG frame on this device: "
            "{0}".format(e)
        )
    png_path = "{0}.dda_decoded.png".format(file_path)
    try:
        with Image.open(file_path) as im:
            ImageOps.exif_transpose(im.convert("RGB")).save(png_path)
    except Exception as e:  # noqa: BLE001 - surface a clear cause
        raise FrameSourceError(
            "Could not decode image '{0}' to PNG: {1}".format(file_path, e)
        )
    return png_path


#: The workflow_core arch id whose CSI/folder file source is read through
#: a Pillow-staged PNG (the JetPack 6 libdlr/libjpeg collision path).
_ARCH_ARM64_JP6 = "arm64_jp6"

#: Designer node type ids of the two typed camera inputs
#: (csi-icam-input-nodes). Distinct from the legacy runtime ImageSourceType
#: strings (NvidiaCSI / ICam).
_CSI_CAMERA_SOURCE_TYPE_ID = "csi_camera_source"
_ICAM_SOURCE_TYPE_ID = "icam_source"


@dataclass(frozen=True)
class CsiCapture:
    """One planned NVIDIA CSI capture: the executor writes the effective
    ``gain``/``exposure`` to the CSI host service config before the
    pipeline runs, then reads the service-staged frame at the compiled
    file path (csi-icam-input-nodes Requirements 7.1, 7.2)."""

    node_id: str
    gain: int
    exposure: int


@dataclass(frozen=True)
class CapturePlan:
    """How a compiled document's typed camera inputs are handled per run.

    ``csi_nodes`` need config-write + (JP6) PNG staging before the
    pipeline runs; ``icam_nodes`` are captured directly through the
    compiled ``v4l2src`` pipeline with no frame-source staging. A document
    with neither takes the exact pre-feature path
    (csi-icam-input-nodes Requirements 7.3, 7.5)."""

    csi_nodes: List[CsiCapture] = field(default_factory=list)
    icam_nodes: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.csi_nodes and not self.icam_nodes


def _effective_int(value, default: int) -> int:
    """Coerce a rendered/resolved parameter to int, falling back to the
    descriptor default when it is missing or non-numeric."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _effective_csi_values(point: dict, node_id, resolution) -> dict:
    """The CSI node's effective parameters: the resolution's resolved
    params when a binding was resolved for the node (csi_assignments),
    else the binding point's rendered ``parameters`` — the compiled-in
    defaults that run when no binding was supplied (Requirement 7.1)."""
    if resolution is not None:
        assignments = getattr(resolution, "csi_assignments", None) or {}
        assignment = assignments.get(node_id)
        if isinstance(assignment, dict):
            params = assignment.get("params")
            if isinstance(params, dict):
                return params
    parameters = point.get("parameters")
    return parameters if isinstance(parameters, dict) else {}


def plan_capture_sources(document: dict, arch: str,
                         resolution=None) -> CapturePlan:
    """Plan the typed camera-input handling for one compiled document.
    Pure over its inputs (csi-icam-input-nodes design Component 5).

    Reads the packager's ``bindingPoints`` section: every
    ``csi_camera_source`` node contributes a :class:`CsiCapture` carrying
    its effective ``gain``/``exposure``; every ``icam_source`` node
    contributes its node id (direct ``v4l2src`` capture, no staging).
    Documents with no ``bindingPoints`` — every pre-feature component —
    and documents with neither typed input yield an empty plan, so the
    executor takes the exact pre-feature path (Requirement 7.5).
    """
    binding_points = (
        document.get("bindingPoints") if isinstance(document, dict) else None
    )
    csi_nodes: List[CsiCapture] = []
    icam_nodes: List[str] = []
    for point in binding_points or []:
        if not isinstance(point, dict):
            continue
        node_type = point.get("nodeType")
        node_id = point.get("nodeId")
        if node_type == _CSI_CAMERA_SOURCE_TYPE_ID:
            values = _effective_csi_values(point, node_id, resolution)
            csi_nodes.append(CsiCapture(
                node_id=node_id,
                gain=_effective_int(
                    values.get("gain"), csi_capture.DEFAULT_GAIN),
                exposure=_effective_int(
                    values.get("exposure"), csi_capture.DEFAULT_EXPOSURE),
            ))
        elif node_type == _ICAM_SOURCE_TYPE_ID:
            icam_nodes.append(node_id)
    return CapturePlan(csi_nodes=csi_nodes, icam_nodes=icam_nodes)


def load_trigger_context(raw: Optional[str]) -> Dict[str, Any]:
    """``trigger_context_json`` -> the run's Trigger_Context. Pure, total.

    The Trigger_Runtime persists the context that started a triggered run
    as JSON in ``workflow_executions.trigger_context_json``; this is the
    read-back half (custom-python-source Requirements 2.1-2.4):

    - a serialized JSON object is deserialized and its entries reproduced
      (Req 2.1);
    - NULL / empty / non-JSON input and serialized non-object JSON yield
      ``{}`` — never an exception — so the run proceeds exactly as today
      (Req 2.2);
    - when the context carries a ``payload`` string (the MQTT shape),
      ``payload_json`` is added holding the payload parsed as JSON when
      it parses, and ``None`` otherwise (Req 2.3, 2.4). Contexts without
      a ``payload`` string (OPC UA, manual) pass through unchanged.
    """
    if not raw or not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    context = dict(parsed)
    payload = context.get("payload")
    if isinstance(payload, str):
        try:
            context["payload_json"] = json.loads(payload)
        except (ValueError, TypeError):
            context["payload_json"] = None
    return context


EXECUTION_STATUS_PENDING = "pending"
EXECUTION_STATUS_RUNNING = "running"
EXECUTION_STATUS_COMPLETED = "completed"
EXECUTION_STATUS_FAILED = "failed"

#: Post-run handler signature: (registration, compiled_document,
#: tag_values) -> None. Task 12.4 registers the executor-binding
#: processor (digital output / MQTT / OPC UA) here.
PostRunHandler = Callable[[WorkflowRegistration, dict, dict], None]


class _NullLatencyMetrics:
    """No-op stand-in for the Pipeline_Configuration LatencyMetrics.

    ``GstPipelineManager.parse_msg`` records an inference-received
    timestamp on the latency metrics while parsing emltriton tags;
    workflow runs have no capture-id-keyed latency records, so a no-op
    keeps the inherited tag parsing intact without writing to the
    Pipeline_Configuration latency tables (Requirement 13.4).
    """

    def add_timestamp(self, name):  # noqa: D102 - interface shim
        return time.time()


def _default_pipeline_manager_factory():
    """A fresh GstPipelineManager per run.

    Imported lazily so this module (and its tests) stay importable
    without GStreamer. A separate instance per run guarantees workflow
    execution never shares state with the Pipeline_Configuration
    manager in ``gst_pipeline_executor`` (Requirements 13.1, 13.4).
    """
    from gstreamer.gst_pipeline import GstPipelineManager

    return GstPipelineManager()


def _default_frame_grabber(camera_id, config):
    """One Aravis frame through the existing Camera_Manager.

    Imported lazily — exactly like ``_default_pipeline_manager_factory``
    and ``GstPipelineManager`` — so this module (and its tests) stay
    importable without the ``gi``/Aravis runtime. Uses the cached,
    persistent connection model ``get_camera_frame`` provides
    (aravis-camera-input Requirement 6.4).
    """
    from utils import camera_manager

    return camera_manager.get_camera_frame(camera_id, config)


def _default_camera_config_resolver(session, camera_id):
    """The device's own Image_Source configuration for ``camera_id``, or
    ``None`` when the camera has no locally configured Image_Source.

    A workflow run must respect the Aravis camera settings the operator
    configured on the device (gain, exposure, advanced GenICam controls)
    — the same ``imageSourceConfiguration`` every classic preview /
    capture / digital-input grab applies — instead of only the workflow's
    compiled-in defaults. Imported lazily (dao/utils) and fully
    contained: any lookup failure returns None and the caller falls back
    to the planned feed config."""
    try:
        from dao.sqlite_db import image_source_dao
        from utils import utils as _server_utils

        source_ids = sorted(
            image_source_dao.list_image_source_ids_by_camera(
                session, camera_id
            )
            or []
        )
        if not source_ids:
            return None
        image_source = image_source_dao.get_image_source(
            session, source_ids[0]
        )
        configuration = getattr(
            image_source, "imageSourceConfiguration", None
        )
        if configuration is None:
            return None
        config = _server_utils.convert_sqlalchemy_object_to_dict(
            configuration
        )
        return config if isinstance(config, dict) and config else None
    except Exception:  # noqa: BLE001 - lookup is best-effort
        logger.debug(
            "Local Image_Source configuration lookup failed for camera "
            "'%s'; the workflow's planned feed config applies",
            camera_id,
            exc_info=True,
        )
        return None


class WorkflowExecutor:
    """Executes pending workflow runs dispatched by the executor hook."""

    def __init__(
        self,
        session_factory: Optional[Callable] = None,
        pipeline_manager_factory: Optional[Callable] = None,
        post_run_handler: Optional[PostRunHandler] = None,
        bridged_pipeline_runner: Optional[Callable] = None,
        bedrock_processor: Optional[BedrockInferenceProcessor] = None,
        llm_processor: Optional[LlmInferenceProcessor] = None,
        binding_resolution_provider: Optional[Callable] = None,
        frame_grabber: Optional[Callable] = None,
        camera_config_resolver: Optional[Callable] = None,
    ) -> None:
        if session_factory is None:
            # Imported lazily so the module is importable without the
            # COMPONENT_WORK_PATH environment the DAO layer requires.
            from dao.sqlite_db.sqlite_db_operations import SessionLocal

            session_factory = SessionLocal
        self._session_factory = session_factory
        self._pipeline_manager_factory = (
            pipeline_manager_factory or _default_pipeline_manager_factory
        )
        self._post_run_handler = post_run_handler
        # Runs launch strings containing Custom_Python_Node bridges
        # (appsink/appsrc pairs pumped through handler subprocesses,
        # Requirement 9.8). Injectable for tests without GStreamer.
        self._bridged_pipeline_runner = (
            bridged_pipeline_runner or python_bridge.run_bridged_pipeline
        )
        # Runs bedrock_inference bindings between the pipeline run and
        # the output bindings: the compiled pipeline captured the two
        # input frames; the processor calls the Bedrock runtime and
        # merges {is_anomalous, confidence} into the tag values the
        # post-run handler gates on. Injectable for tests without boto3.
        # When the post-run handler is the run's OutputBindingProcessor
        # (it exposes ``process_subset``), it is injected as the Bedrock
        # processor's ``output_processor`` seam, activating the
        # concurrent publish-on-completion path — each Bedrock branch's
        # output bindings run as its result lands (detection-guided-
        # bedrock-inspection Requirements 5.2, 5.3). Handlers without
        # ``process_subset`` (plain lambdas, other implementations) keep
        # the sequential legacy path byte-identical to today.
        if bedrock_processor is None:
            output_processor = (
                post_run_handler
                if callable(getattr(post_run_handler, "process_subset", None))
                else None
            )
            bedrock_processor = BedrockInferenceProcessor(
                output_processor=output_processor
            )
        self._bedrock_processor = bedrock_processor
        # Branch-scoped output bindings publish inside the Bedrock
        # processor's completion path exactly when it holds an output
        # processor; the post-run handler must then exclude them so they
        # never run twice, while non-branch bindings keep today's
        # post-run ordering (Requirement 5.7).
        self._bedrock_branch_publish = (
            getattr(self._bedrock_processor, "_output_processor", None)
            is not None
        )
        # Runs llm_inference bindings after the Bedrock processor and
        # before the output bindings: renders each binding's
        # Prompt_Template from the run metadata, calls the device
        # Text_Generation_API, and merges the outcome under
        # metadata['llm'][nodeId]. Binding failures are recorded, never
        # raised (vllm-triton-inference Requirements 7.3-7.7).
        # Injectable for tests without HTTP.
        self._llm_processor = llm_processor or LlmInferenceProcessor()
        # Camera_Binding resolution lookup: registration id ->
        # Optional[ResolutionResult] (the watcher's binding_resolution()
        # accessor in production). When it returns a resolution the run
        # executes the resolution's substituted document; None (or a
        # provider failure, logged) falls back to the on-disk document
        # (aravis-camera-input Requirement 6.4).
        self._binding_resolution_provider = binding_resolution_provider
        # One Aravis frame per planned feed: (camera_id, config) ->
        # {'data','height','width'}. Injectable for tests without the
        # gi/Aravis runtime (Requirements 6.4, 6.5).
        self._frame_grabber = frame_grabber or _default_frame_grabber
        # (session, camera_id) -> the device's Image_Source configuration
        # for the camera (or None). Injectable for tests; the default
        # reads the local DB so a workflow grab always respects the
        # operator's configured camera settings.
        self._camera_config_resolver = (
            camera_config_resolver or _default_camera_config_resolver
        )

    def set_post_run_handler(self, handler: Optional[PostRunHandler]) -> None:
        """Register the post-pipeline output-binding processor (task 12.4)."""
        self._post_run_handler = handler

    @staticmethod
    def _stage_frame_sources(document: dict) -> List[FolderFrame]:
        """Resolve each file-based image source to a concrete frame file,
        mirroring the Pipeline_Configuration builder's FOLDER source
        (``_add_file_image_source``).

        The portal ``folder_source`` widget compiles to a ``filesrc`` chain,
        but ``filesrc`` reads a single file — pointing it at a directory
        fails with ``"<dir>" is a directory``. So a directory ``location``
        is resolved to the oldest JPEG inside it. And when the compiled
        decode chain expects a PNG (the JetPack 6 staged path, ``pngdec`` to
        dodge the libdlr/libjpeg collision) the selected JPEG is
        Pillow-decoded to a staged PNG with EXIF orientation baked in.
        Single-file locations whose decoder already matches (the JP5/x86
        ``jpegdec`` chains) are left untouched. Mutates the per-run document
        in place; raises :class:`FrameSourceError` (carrying the source
        node id) when a source can't be resolved.

        Returns the :class:`FolderFrame` recorded for every
        directory-resolved location, so ``execute()`` can consume the frame
        after a successful pipeline run or relocate it to ``failed/`` on
        failure (folder-source-image-consumption Reqs 2.1-2.5).
        Single-file locations are NOT recorded — they are never consumed,
        exactly like the legacy FILE-type sources (Req 3.1). When JP6 PNG
        staging fails for a directory-resolved JPEG the raised
        :class:`FrameSourceError` carries the resolved original path in
        ``source_image`` so the caller can relocate the bad image (Req
        2.4)."""
        folder_frames: List[FolderFrame] = []
        for segment in document.get("segments", []):
            elements = segment.get("elements", [])
            if not isinstance(elements, list):
                continue
            # The JP6 staged chain decodes with pngdec and therefore needs a
            # PNG on disk; the JP5/x86 chains decode JPEG directly.
            expects_png = any(
                isinstance(el, dict) and el.get("factory") == "pngdec"
                for el in elements
            )
            for element in elements:
                if (
                    not isinstance(element, dict)
                    or element.get("factory") != "filesrc"
                ):
                    continue
                args = element.get("args")
                if not isinstance(args, dict):
                    continue
                location = args.get("location")
                if not location or not isinstance(location, str):
                    continue
                node_id = element.get("nodeId")
                resolved = location
                staged_png: Optional[str] = None
                from_directory = False
                try:
                    if os.path.isdir(location):
                        resolved = _oldest_image_in_folder(location)
                        from_directory = True
                    if expects_png and not resolved.lower().endswith(".png"):
                        staged_png = _stage_decoded_png(resolved)
                except FrameSourceError as e:
                    if e.node_id is None:
                        e.node_id = node_id
                    if from_directory:
                        # A directory-resolved JPEG failed to decode/stage:
                        # hand the caller the bad image so it can be
                        # relocated to failed/ instead of wedging every
                        # subsequent run (Req 2.4).
                        e.source_image = resolved
                    raise
                if from_directory:
                    folder_frames.append(FolderFrame(
                        original=resolved,
                        staged_png=staged_png,
                        node_id=node_id,
                    ))
                final = staged_png or resolved
                if final != location:
                    logger.info(
                        "Resolved workflow frame source '%s' to '%s'",
                        location,
                        final,
                    )
                    args["location"] = final
        return folder_frames

    @staticmethod
    def _consume_folder_frames(folder_frames: List[FolderFrame]) -> None:
        """Delete every directory-resolved frame's staged PNG and original
        JPEG after a successful pipeline run, mirroring the legacy
        ``_cleanup_file_after_processing`` so the folder drains
        (folder-source-image-consumption Reqs 2.1, 2.2, 2.3).

        Runs immediately after the pipeline run returns — BEFORE the
        Bedrock/LLM/output-binding steps — so consumption happens
        regardless of downstream outcomes, exactly like the legacy
        ``else`` branch. Entirely best-effort and contained: a delete
        failure is logged per file and never fails a completed run. A
        no-op for the empty list, so non-folder runs take the exact
        pre-fix path."""
        if not folder_frames:
            return
        try:
            # Lazy like the other DAO/gstreamer imports so this module
            # stays importable on hosts without the fastapi/camera stack.
            from utils import captured_images_utils

            delete_image = captured_images_utils.delete_image
        except Exception:  # noqa: BLE001 - contained, fall back to os
            logger.exception(
                "captured_images_utils unavailable; consuming folder "
                "frames via os.remove"
            )
            delete_image = os.remove
        for frame in folder_frames:
            for path in (frame.staged_png, frame.original):
                if not path:
                    continue
                try:
                    delete_image(path)
                    logger.info(
                        "Consumed processed folder-source file: %s", path
                    )
                except Exception:  # noqa: BLE001 - contained per 13.7
                    logger.exception(
                        "Could not consume processed folder-source file "
                        "%s; continuing",
                        path,
                    )

    @staticmethod
    def _relocate_failed_folder_frames(
        workflow_id: str, folder_frames: List[FolderFrame]
    ) -> None:
        """Relocate every directory-resolved frame's original JPEG to
        ``{INFERENCE_RESULTS_DIR}/{workflow_id}/failed/`` after a failed
        run, mirroring the legacy ``_move_bad_folder_image_source`` so a
        bad image never wedges the folder (folder-source-image-consumption
        Reqs 2.4, 2.5). Staged PNGs are best-effort removed.

        Entirely best-effort and contained: any per-file failure is
        logged and never masks the run's real failure. A no-op for the
        empty list, so non-folder runs take the exact pre-fix path."""
        if not folder_frames:
            return
        try:
            from utils import constants

            failed_dir = os.path.join(
                constants.INFERENCE_RESULTS_DIR, workflow_id, "failed"
            )
        except Exception:  # noqa: BLE001 - contained per 13.7
            logger.exception(
                "Could not resolve the failed-images directory for "
                "workflow %s; leaving folder-source images in place",
                workflow_id,
            )
            return
        if not os.path.isdir(failed_dir):
            try:
                # The legacy directory creation (chown/chmod to the DDA
                # user); lazy import for host-test importability.
                from utils import dda_user_management_utils

                dda_user_management_utils.create_dda_user_directory(
                    failed_dir
                )
            except Exception:  # noqa: BLE001 - contained per 13.7
                logger.exception(
                    "create_dda_user_directory failed for %s; falling "
                    "back to os.makedirs",
                    failed_dir,
                )
                try:
                    os.makedirs(failed_dir, exist_ok=True)
                except OSError:
                    logger.exception(
                        "Could not create the failed-images directory "
                        "%s; leaving folder-source images in place",
                        failed_dir,
                    )
                    return
        for frame in folder_frames:
            try:
                relocated = os.path.join(
                    failed_dir, os.path.basename(frame.original)
                )
                os.rename(frame.original, relocated)
                logger.info(
                    "Relocated failed folder-source image %s to %s",
                    frame.original,
                    relocated,
                )
            except Exception:  # noqa: BLE001 - contained per 13.7
                logger.exception(
                    "Could not relocate failed folder-source image %s; "
                    "continuing",
                    frame.original,
                )
            if frame.staged_png:
                try:
                    os.remove(frame.staged_png)
                except OSError:
                    logger.debug(
                        "Could not remove staged PNG %s; continuing",
                        frame.staged_png,
                        exc_info=True,
                    )

    @staticmethod
    def _prepare_csi_captures(plan: "CapturePlan", arch: str) -> None:
        """Write the CSI host service config and stage the CSI frame for
        each planned ``csi_camera_source`` node before the pipeline runs
        (csi-icam-input-nodes Requirements 7.1, 7.2, 7.4).

        For every CSI node the effective ``gain``/``exposure`` are written
        to ``/aws_dda/nvidia-csi-capture/config.json`` (tolerant: a write
        failure is logged and swallowed, the service keeps its last-known
        settings — Requirement 7.1). On ``arm64_jp6`` the service-staged
        ``latest.jpg`` is Pillow-decoded to ``latest.jpg.dda_decoded.png``
        at the compiled read path (Requirement 7.2). A missing/unreadable
        capture frame raises :class:`FrameSourceError` carrying the CSI
        node id so the run fails attributed to that node (Requirement 7.4).
        ``icam_source`` nodes need no staging — the compiled ``v4l2src``
        pipeline captures live (Requirement 7.3)."""
        for csi in plan.csi_nodes:
            # Effective acquisition settings for the CSI host service.
            csi_capture.write_csi_config(gain=csi.gain, exposure=csi.exposure)
            source = csi_capture.CSI_LATEST_JPG
            if not os.path.isfile(source) or not os.access(source, os.R_OK):
                raise FrameSourceError(
                    "NVIDIA CSI capture frame '{0}' is missing or unreadable; "
                    "the CSI capture service has not staged a frame".format(
                        source),
                    node_id=csi.node_id,
                )
            if arch == _ARCH_ARM64_JP6:
                # JP6 reads a Pillow-staged PNG (pngdec chain) to dodge the
                # libdlr/libjpeg collision, exactly like the folder source's
                # JP6 staging (_stage_frame_sources).
                try:
                    _stage_decoded_png(source)
                except FrameSourceError as e:
                    if e.node_id is None:
                        e.node_id = csi.node_id
                    raise

    @staticmethod
    def _ensure_terminal_sink(document: dict) -> None:
        """Append a ``fakesink`` to any terminal branch that ends in a
        src-pad-bearing element (e.g. ``emlcapture``).

        The portal capture output node compiles to ``jpegenc ! emlcapture``
        with no terminal sink, but ``emlcapture`` has a src pad — the
        on-device builder always follows it with ``fakesink``
        (``_add_post_processing_plugins``). Left dangling, the src pad's
        push returns ``GST_FLOW_NOT_LINKED`` and the run fails right after
        inference. Only branches that don't feed a funnel (``linkTo``) and
        that end in a known src-pad element are touched, so already-correct
        pipelines (and those ending in a real sink) are unchanged."""
        for segment in document.get("segments", []):
            if segment.get("linkTo"):
                continue
            elements = segment.get("elements")
            if not isinstance(elements, list) or not elements:
                continue
            last = elements[-1]
            if (
                isinstance(last, dict)
                and last.get("factory") in _SRC_PAD_TERMINAL_FACTORIES
            ):
                elements.append(
                    {
                        "factory": "fakesink",
                        "nodeId": last.get("nodeId"),
                        "args": {},
                    }
                )

    @staticmethod
    def _inject_inference_metadata(
        document: dict,
        workflow_id: str,
        execution_id: str,
        output_dir: str,
    ) -> None:
        """Give every ``emltriton`` model-inference element the ``METADATA``
        input the DDA ensemble models require.

        The cookies-style ensembles have a marshal step that reads a JSON
        ``metadata`` tensor (``capture_id``, the capture-data disk path, and
        the device fleet name — the SageMaker-edge/em-agent config the
        on-device ``_add_inference_plugins`` loads from disk and injects the
        capture id into). The portal compiler emits no ``metadata`` arg, so
        Triton rejects the request with *missing required input(s)
        ['METADATA']* and the pipeline never produces a result. Mirror the
        on-device builder: synthesize a per-run ``capture_id`` and point the
        capture-data disk path at the per-run ``output_dir`` (see §2 of the
        design). The marshal model derives its workflow id from
        ``basename(disk_path)``, so pointing it at ``output_dir`` keeps that
        derivation and the emlcapture file targets
        (:meth:`_route_capture_outputs`, which reuses the same ``output_dir``
        / ``capture_id``) consistent. ``render_value`` handles the JSON
        quoting. Only fills args that aren't already present, so an
        explicitly compiled ``metadata``/``correlation-id`` wins, and models
        without a marshal metadata input simply ignore the extra property."""
        capture_id = "{0}-{1}".format(workflow_id, execution_id)
        disk_path = output_dir
        metadata = json.dumps(
            {
                "capture_id": capture_id,
                "sagemaker_edge_core_capture_data_disk_path": disk_path,
                "sagemaker_edge_core_device_fleet_name": "",
            }
        )
        for segment in document.get("segments", []):
            for element in segment.get("elements", []):
                if (
                    not isinstance(element, dict)
                    or element.get("factory") != "emltriton"
                ):
                    continue
                args = element.get("args")
                if not isinstance(args, dict):
                    continue
                # Only models that declare a METADATA input need it; leave
                # plain models (and the fixture repos in tests) untouched.
                if not _model_declares_metadata_input(args.get("model")):
                    continue
                injected = False
                if "metadata" not in args:
                    args["metadata"] = metadata
                    injected = True
                if "correlation-id" not in args:
                    args["correlation-id"] = capture_id
                if injected:
                    logger.info(
                        "Injected inference metadata (capture_id=%s) for "
                        "emltriton model '%s'",
                        capture_id,
                        args.get("model"),
                    )

    @staticmethod
    def _route_capture_outputs(
        document: dict, output_dir: str, capture_id: str
    ) -> bool:
        """Route a capture document's outputs to the per-run artifact
        location, returning whether the document is a File_Output_Node run.

        The portal capture output node compiles to a trailing ``emlcapture``
        whose ``meta`` is empty/``{capture_meta}``, so overlay/mask/jsonl are
        never written. Mirror the on-device builder
        (``gstreamer.pipeline_builder._add_post_processing_plugins``):
        populate that ``meta`` with the ``triton_inference_output_*`` routing
        string targeting ``{output_dir}/{capture_id}`` via the message-broker
        ``file-target_`` convention. Only the targets whose matching output
        the deployed model declares in its ``config.pbtxt`` are added (via
        :func:`_model_declared_outputs`), so overlay-less/plain models route
        only their applicable targets and models declaring none (or with no
        readable config, e.g. the fixture repos in tests) get no ``meta``.

        Mutates only a *terminal* ``emlcapture`` element (the last element of
        a segment that doesn't feed a funnel), so documents whose terminal
        node is not a capture node render byte-identically to today
        (Property 3). The additive ``is_anomalous``/``confidence`` tag values
        still come from the emltriton tags exactly as before (Property 4).

        Returns True when the document's terminal node is a File_Output_Node
        (drives ``has_image_results``, R1.4/R5.1); False for a non-capture
        document, which is left untouched (R1.5)."""
        terminal_captures = []
        for segment in document.get("segments", []):
            if segment.get("linkTo"):
                continue
            elements = segment.get("elements")
            if not isinstance(elements, list) or not elements:
                continue
            last = elements[-1]
            if isinstance(last, dict) and last.get("factory") == "emlcapture":
                terminal_captures.append(last)
        if not terminal_captures:
            # Not a File_Output_Node run: leave the document untouched.
            return False

        # Enumerate the outputs the run's deployed model(s) declare, so only
        # applicable routing targets are emitted (R1.3, R1.5).
        declared: set = set()
        for segment in document.get("segments", []):
            for element in segment.get("elements", []):
                if (
                    isinstance(element, dict)
                    and element.get("factory") == "emltriton"
                ):
                    args = element.get("args")
                    if isinstance(args, dict):
                        declared |= _model_declared_outputs(args.get("model"))

        # The message broker resolves a ``file-target_{DIR}-{ext}`` target to
        # ``{DIR}/{capture_id}.{ext}`` (correlation-id == capture_id — see
        # test/backend-test/test_overlay_path_consistency.py). So the ``{p}``
        # prefix is the per-run ``output_dir`` itself (NOT
        # ``output_dir/capture_id``); the broker appends ``{capture_id}.{ext}``,
        # matching the marshal's ``source-ref`` and what ``run_artifacts``
        # resolves. This mirrors the on-device builder, whose targets use the
        # bare ``workflowOutputPath`` folder.
        base_target = "file-target_{0}-jpg".format(output_dir)
        meta = ",".join(
            _CAPTURE_OUTPUT_TARGETS[name].format(p=output_dir)
            for name in _CAPTURE_OUTPUT_ORDER
            if name in declared
        )
        for capture in terminal_captures:
            args = capture.setdefault("args", {})
            if not isinstance(args, dict):
                continue
            # Point the base captured frame at {output_dir}/{capture_id}.jpg so
            # it lands in the per-run dir (the portal compiles a fixed default
            # buffer-message-id that writes elsewhere), aligning the on-disk
            # base image with the marshal source-ref and run_artifacts.
            args["buffer-message-id"] = base_target
            if meta:
                existing = args.get("meta")
                if not existing or existing == _CAPTURE_META_PLACEHOLDER:
                    args["meta"] = meta
            logger.info(
                "Routed capture outputs for node %s to %s (buffer=%s, meta=%s)",
                capture.get("nodeId"),
                output_dir,
                base_target,
                meta or "<none>",
            )
        return True

    @staticmethod
    def _resolve_model_names(document: dict) -> None:
        """Rewrite every ``emltriton`` element's ``model`` arg from the
        workflow's registry model name to the deployed Triton model name
        (see :func:`resolve_triton_model_name`). Mutates the per-run
        document in place; a no-op when the model repo is empty/unreadable
        or the name already matches. Logs each resolution, and warns when a
        model can't be matched to anything deployed so the cause is obvious
        in the run logs rather than only as a Triton "failed to load"."""
        loaded = _loaded_ensemble_models()
        if not loaded:
            return
        for segment in document.get("segments", []):
            for element in segment.get("elements", []):
                if element.get("factory") != "emltriton":
                    continue
                args = element.get("args")
                if not isinstance(args, dict):
                    continue
                model_name = args.get("model")
                resolved = resolve_triton_model_name(model_name, loaded)
                if resolved != model_name:
                    logger.info(
                        "Resolved workflow model '%s' to deployed Triton "
                        "model '%s'",
                        model_name,
                        resolved,
                    )
                    args["model"] = resolved
                elif model_name and model_name not in loaded:
                    logger.warning(
                        "Workflow model '%s' matches no deployed Triton model "
                        "%s; leaving unchanged (inference will fail unless the "
                        "model is deployed)",
                        model_name,
                        loaded,
                    )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, execution_id: str) -> None:
        """Run one pending execution end to end.

        Runs on the hook's daemon thread. Every failure mode ends with
        the execution row marked ``failed`` (with the failing node when
        identifiable) and nothing raised (Requirements 9.7, 13.7).
        """
        session = self._session_factory()
        work_dir: Optional[str] = None
        log_capture: Optional[RunLogCapture] = None
        # Initialized alongside log_capture so the finally block can
        # reference the collector safely on every terminal path
        # (node-execution-timing R4.1); reassigned by _begin_node_status.
        collector: Optional[NodeStatusCollector] = None
        try:
            execution = session.get(WorkflowExecution, execution_id)
            if execution is None:
                logger.error(
                    "Workflow execution %s was not found; nothing to run",
                    execution_id,
                )
                return
            if execution.status != EXECUTION_STATUS_PENDING:
                logger.warning(
                    "Workflow execution %s is '%s', not pending; skipping",
                    execution_id,
                    execution.status,
                )
                return

            # The run's Trigger_Context (custom-python-source Requirements
            # 2.1-2.4): read back what the Trigger_Runtime persisted for
            # this execution. Total — NULL/empty/invalid rows yield {} and
            # the run proceeds exactly as today.
            trigger_context = load_trigger_context(
                getattr(execution, "trigger_context_json", None)
            )

            registration = session.get(
                WorkflowRegistration, execution.registration_id
            )
            failure = self._preflight(registration)
            if failure is not None:
                self._finish_failed(session, execution, error=failure)
                return

            # Capture this run's log from as early as possible now that the
            # registration (and therefore the workflow id) is known. The log
            # lives alongside the run's artifacts at
            # {_WORKFLOW_CAPTURE_ROOT}/{workflow_id}/{execution_id}/run.log
            # so every started run — capture or not — has a retrievable log
            # (Requirements 2.1, 2.5). Best-effort and contained: capture
            # setup never fails the run (Requirement 2.6).
            log_capture = self._begin_log_capture(
                session, execution, registration
            )

            document, load_error = self._load_compiled_document(registration)
            if load_error is not None:
                self._finish_failed(session, execution, error=load_error)
                return

            # Camera_Binding resolution (aravis-camera-input Requirement
            # 6.4): when the provider carries a resolution for this
            # registration, the run executes its slot-substituted document
            # (a private copy — the watcher's cache is never mutated);
            # otherwise the on-disk document runs exactly as before.
            resolution = self._binding_resolution(registration)
            if resolution is not None and isinstance(
                getattr(resolution, "document", None), dict
            ):
                document = copy.deepcopy(resolution.document)

            # Aravis frame feed (Requirements 6.4, 6.5, 6.6): plan the
            # document's Aravis feed, grab its frame through the camera
            # manager, and point the compiled appsrc at the Frame_Feed.
            # Planning and grab failures fail this run with the Aravis
            # node identified; Aravis-free documents plan zero feeds and
            # take the exact pre-feature path.
            try:
                frame_data = self._prepare_aravis_frame_feed(
                    session, document, resolution
                )
            except AravisFeedError as e:
                logger.error(
                    "Workflow execution %s failed in the Aravis frame feed "
                    "(node %s): %s",
                    execution_id,
                    e.node_id or "unidentified",
                    e,
                )
                self._finish_failed(
                    session,
                    execution,
                    error=str(e),
                    failing_node_id=e.node_id,
                )
                return

            # Custom Python source feed (custom-python-source Requirements
            # 7.1-7.6): plan the document's Frame_Producer, run
            # produce_frame(context) in its handler subprocess, and point
            # the compiled appsrc at the Frame_Feed — the same single-frame
            # model the Aravis feed uses (the planner guarantees the two
            # can never coexist). Planning and production failures fail
            # this run with the node identified BEFORE the pipeline
            # starts; source-free documents plan zero feeds and take the
            # exact pre-feature path.
            try:
                python_frame_data, producer_metadata = (
                    self._prepare_python_source_feed(
                        document, registration, trigger_context
                    )
                )
            except (
                PythonSourceError, python_bridge.CustomPythonNodeError
            ) as e:
                failing_node_id = getattr(e, "node_id", None)
                logger.error(
                    "Workflow execution %s failed in the Custom Python "
                    "source feed (node %s): %s",
                    execution_id,
                    failing_node_id or "unidentified",
                    e,
                )
                self._finish_failed(
                    session,
                    execution,
                    error=str(e),
                    failing_node_id=failing_node_id,
                )
                return
            if python_frame_data is not None:
                frame_data = python_frame_data

            # Custom_Python_Node bridges (Requirement 9.8): replace each
            # emlpython element with the executor-managed appsink/appsrc
            # pair before rendering; the pair keeps the node's id so
            # failures map back to it.
            bridge_specs = python_bridge.bridge_specs(document)
            # Custom nodes stream-downstream of a model_inference element
            # get the run's Detection_List injected by the bridge pump
            # (detection-guided-bedrock-inspection Requirement 1.10);
            # computed BEFORE the rewrite replaces the emlpython elements
            # the topology scan discriminates on.
            detection_downstream_ids = (
                python_bridge.detection_downstream_node_ids(document)
                if bridge_specs
                else frozenset()
            )
            if bridge_specs:
                document = python_bridge.rewrite_document(document)

            # Per-run working directory for {work_dir}-rooted artifacts
            # (bedrock_inference frame-capture sinks); resolved into the
            # element args before rendering, exactly like the harness
            # resolves {dataset_location}. Removed after the run.
            work_dir = self._prepare_work_dir(document)

            # Typed camera-input handling (csi-icam-input-nodes Component
            # 5): write the CSI host service config with each CSI node's
            # effective gain/exposure and (on JP6) stage its capture frame
            # as a decoded PNG before the pipeline runs; a missing capture
            # frame fails the run attributed to the CSI node. ICAM nodes and
            # documents with neither typed input plan no work and take the
            # exact pre-feature path.
            capture_plan = plan_capture_sources(
                document, registration.arch, resolution
            )
            try:
                self._prepare_csi_captures(capture_plan, registration.arch)
            except FrameSourceError as e:
                logger.error(
                    "Workflow execution %s failed preparing its CSI capture "
                    "(node %s): %s",
                    execution_id,
                    e.node_id or "unidentified",
                    e,
                )
                self._finish_failed(
                    session,
                    execution,
                    error=str(e),
                    failing_node_id=e.node_id,
                )
                return

            # Resolve file/folder image sources to a concrete frame file the
            # same way the Pipeline_Configuration builder does: a directory
            # location becomes the oldest JPEG inside it, and the JP6 pngdec
            # chain gets a Pillow-staged PNG. Without this the compiled
            # filesrc is handed a directory (or a raw JPEG the pngdec chain
            # can't read) and the pipeline never reaches PLAYING.
            try:
                folder_frames = self._stage_frame_sources(document)
            except FrameSourceError as e:
                logger.error(
                    "Workflow execution %s failed resolving its image source "
                    "(node %s): %s",
                    execution_id,
                    e.node_id or "unidentified",
                    e,
                )
                # A directory-resolved JPEG that failed to decode/stage is
                # relocated to failed/ so it cannot wedge every subsequent
                # run (folder-source-image-consumption Req 2.4).
                bad_image = getattr(e, "source_image", None)
                if bad_image:
                    self._relocate_failed_folder_frames(
                        registration.workflow_id,
                        [FolderFrame(
                            original=bad_image,
                            staged_png=None,
                            node_id=e.node_id,
                        )],
                    )
                self._finish_failed(
                    session,
                    execution,
                    error=str(e),
                    failing_node_id=e.node_id,
                )
                return

            # Resolve each model-inference node's model name (the use-case
            # registry name the portal designer stored, e.g. "cookies-binary")
            # to the Triton model actually deployed on THIS device (e.g.
            # "model-cookies-binary-jetson-xavier-jp6"). Without this the
            # pipeline asks Triton for a model it doesn't have and never
            # reaches PLAYING.
            self._resolve_model_names(document)

            # The per-run artifact location: unique per execution so runs of
            # the same workflow never overwrite each other (R1.2). Both the
            # METADATA disk path and the emlcapture file targets point here,
            # so the marshal model's workflow-id derivation and the written
            # artifacts agree (design §2).
            output_dir = os.path.join(
                _WORKFLOW_CAPTURE_ROOT, registration.workflow_id, execution_id
            )
            capture_id = "{0}-{1}".format(
                registration.workflow_id, execution_id
            )

            # The registration's workflow.json graph document — the
            # executor-read source of model_inference's
            # ``detection_sort_order`` (the parameter never reaches the
            # compiled document) — and the run-state detections cache:
            # the Detection_List is built exactly once per run through
            # this dict, shared between the bridge pump's injector and
            # the post-pipeline merge below, so every consumer sees the
            # same entries with the same Detection_IDs (detection-guided-
            # bedrock-inspection design Property 1).
            graph_document = self._load_graph_document(registration)
            detections_cache: Dict[str, Any] = {}

            # Supply the METADATA input the DDA ensemble models' marshal
            # step requires (capture id / capture-data path / fleet name);
            # without it Triton rejects the request as missing an input.
            self._inject_inference_metadata(
                document, registration.workflow_id, execution_id, output_dir
            )

            # Route a capture document's outputs to the per-run location and
            # record where its artifacts live (R1.1-R1.4). Contained: any
            # failure is logged and swallowed so the run still proceeds
            # (R8.5); non-capture documents are left untouched (R1.5).
            try:
                try:
                    os.makedirs(output_dir, exist_ok=True)
                except OSError:
                    logger.exception(
                        "Could not create per-run artifact dir %s; "
                        "continuing",
                        output_dir,
                    )
                # ALWAYS record where the run's artifacts live — even
                # without a File_Output terminal — so the run metadata
                # JSON and the inference-node frames have a destination
                # (vlm-parity-run-results Requirement 2.3; fixes
                # Bedrock-only runs persisting nothing).
                # ``has_image_results`` stays gated on a routed terminal
                # capture here; ``_persist_node_frames`` additionally
                # sets it when node frames were persisted (Req 2.4).
                execution.capture_id = capture_id
                execution.output_dir = output_dir
                if self._route_capture_outputs(
                    document, output_dir, capture_id
                ):
                    execution.has_image_results = True
                session.commit()
            except Exception:  # noqa: BLE001 - contained per 13.7 / R8.5
                logger.exception(
                    "Workflow execution %s capture-output routing failed; "
                    "the run continues without artifact routing",
                    execution_id,
                )

            # Terminate a bare capture branch with a sink; a trailing
            # emltriton/emlcapture src pad left unlinked fails the run with
            # GST_FLOW_NOT_LINKED right after inference.
            self._ensure_terminal_sink(document)

            launch_string = rendering.render_launch_string(document)
            if not launch_string:
                self._finish_failed(
                    session,
                    execution,
                    error="Compiled pipeline document renders an empty "
                    "pipeline (no elements)",
                )
                return
            name_map = rendering.element_name_map(document)

            # Per-node run-status collection (Requirement 3). Built from the
            # element-name -> nodeId map so bus signals (via the optional
            # run_pipeline status_sink) map back to workflow nodes, and
            # seeded with the document's executorBindings node ids
            # (llm_inference, mqtt_publish, ...) so binding nodes reach a
            # terminal status too — never absent/"pending" in the run view.
            # Contained: a collector error never fails the run (R8.5), so a
            # construction failure just leaves status collection off
            # (collector = None).
            collector = self._begin_node_status(name_map, document)

            execution.status = EXECUTION_STATUS_RUNNING
            execution.started_at = int(time.time())
            session.commit()
            if collector is not None:
                try:
                    collector.mark_running_all()
                except Exception:  # noqa: BLE001 - contained per R8.5
                    logger.debug(
                        "NodeStatusCollector.mark_running_all ignored an error",
                        exc_info=True,
                    )
            logger.info(
                "Workflow execution %s (%s v%s) starting pipeline: %s",
                execution_id,
                registration.workflow_id,
                registration.version,
                launch_string,
            )

            plugin_dir = os.path.join(
                registration.artifact_path, "plugins", registration.arch
            )
            # The manifest names the Plugin_Component install roots that
            # join the plugin scan path and carries the pluginChecksums
            # verified before the registry scan (custom-node-designer
            # Requirements 10.6, 11.4). Best effort: a workflow without
            # a loadable manifest keeps the inline-directory behavior
            # (its registration would be invalid anyway).
            manifest = self._load_manifest(registration)
            try:
                with workflow_plugin_path(
                    plugin_dir,
                    manifest=manifest,
                    artifact_path=registration.artifact_path,
                ):
                    # Preflight: every declared element factory must be
                    # registered now that the workflow plugin scan ran;
                    # a missing one (e.g. a custom node whose declared
                    # factory does not match the element its built
                    # plugin registers) raises with the node identified
                    # instead of an unattributable pre-parse
                    # gst_parse_error.
                    self._preflight_pipeline_factories(
                        document,
                        plugin_dir,
                        manifest,
                        registration.artifact_path,
                    )
                    if bridge_specs:
                        # The merged Frame_Feed (Aravis camera grab OR
                        # Custom Python Produced_Frame — frame_data
                        # carries whichever the document planned) and
                        # pumped emlpython bridges run in the same
                        # pipeline (custom-python-source Requirement
                        # 7.4). Forwarding the merged frame_data (not
                        # the unmerged python_frame_data, which is None
                        # for camera-fed documents) fixes the
                        # camera+bridges 120s stall where the grabbed
                        # frame was dropped and the fed appsrc starved
                        # (bridged-pipeline-camera-frame-feed-stall
                        # Requirements 2.1–2.3); frame_data=None (every
                        # feed-free bridged run) keeps today's exact
                        # call shape (Requirement 3.2). The detections
                        # injector (None for documents with no custom
                        # node downstream of model_inference — the
                        # byte-identical pre-feature path) hands the
                        # pump the run's Detection_List through the
                        # shared run-state cache (Requirement 1.10).
                        tag_values = self._run_bridged(
                            registration,
                            bridge_specs,
                            launch_string,
                            frame_data=frame_data,
                            detections_injector=self._detections_injector(
                                detection_downstream_ids,
                                output_dir,
                                capture_id,
                                graph_document,
                                detections_cache,
                            ),
                        )
                    elif frame_data is not None:
                        # Frame_Feed (Aravis grab or Custom Python
                        # Produced_Frame): run_pipeline locates the
                        # appsrc, wraps the frame, pushes it and
                        # sends EOS — the classic Camera-type execution
                        # model (Requirements 6.4; custom-python-source
                        # 7.1, 7.5).
                        manager = self._pipeline_manager_factory()
                        tag_values = manager.run_pipeline(
                            launch_string,
                            frame_data,
                            latency_metrics=_NullLatencyMetrics(),
                            status_sink=self._status_sink(collector),
                        )
                    else:
                        manager = self._pipeline_manager_factory()
                        tag_values = manager.run_pipeline(
                            launch_string,
                            latency_metrics=_NullLatencyMetrics(),
                            status_sink=self._status_sink(collector),
                        )
            except Exception as e:  # noqa: BLE001 - contained per 13.7
                # A failed pipeline run relocates every directory-resolved
                # source image to failed/ so the folder still drains,
                # mirroring the legacy except branch
                # (folder-source-image-consumption Req 2.5). Contained and
                # best-effort; the existing failure handling is unchanged.
                self._relocate_failed_folder_frames(
                    registration.workflow_id, folder_frames
                )
                # Bridge errors carry the Custom_Python_Node id directly
                # (Requirement 9.8); anything else is mapped from the
                # failing element name (Requirement 9.7).
                failing_node_id = getattr(
                    e, "node_id", None
                ) or rendering.failing_node_id_from_error(name_map, str(e))
                logger.error(
                    "Workflow execution %s failed (node %s): %s",
                    execution_id,
                    failing_node_id or "unidentified",
                    e,
                )
                self._finish_failed(
                    session,
                    execution,
                    error=str(e),
                    failing_node_id=failing_node_id,
                )
                self._persist_node_status(
                    session,
                    execution,
                    collector,
                    failing_node_id=failing_node_id,
                    failure_detail=str(e),
                )
                return

            # Pipeline_EOS terminal marking: run_pipeline returned cleanly,
            # so every running pipeline node is transitioned to success now,
            # freezing its lifecycle duration at EOS instead of at run end
            # (vllm-workflow-latency-optimization R2.1, R2.3, R2.5). The
            # failure/timeout handlers above return before this point, so
            # the existing finalize rules apply unchanged on those paths.
            # Contained internally per the collector's best-effort
            # discipline (R2.6).
            if collector is not None:
                collector.mark_pipeline_success()

            # Mid-run node-status snapshot: the pipeline finished, so
            # pipeline nodes carry terminal statuses (and lifecycle
            # durations) the polling node-status endpoint can serve while
            # the run is still active (node-execution-timing R2.5).
            # Contained best-effort write; the run proceeds on any error.
            self._persist_node_status_snapshot(session, execution, collector)

            # Merge the Frame_Producer's metadata into the Run_Metadata
            # under the node's key (custom-python-source Requirement
            # 6.7) — before the trigger seeding below and therefore
            # before the Bedrock/LLM processors and output bindings.
            # Empty producer metadata (and source-free runs) merge
            # nothing, keeping the source-free Run_Metadata delta to
            # exactly the seeded ``trigger`` key (Requirement 11.1).
            if producer_metadata:
                tag_values.setdefault(
                    "python_source", {}
                ).update(producer_metadata)

            # Seed the Run_Metadata with the run's Trigger_Context under
            # ``trigger`` — on every run path, BEFORE the Bedrock/LLM
            # processors and the output bindings run, and never
            # overwriting a key the pipeline's TAG messages produced
            # (custom-python-source Requirements 2.5, 2.7). Trigger-less
            # runs seed {} — the only Run_Metadata delta allowed by
            # Requirement 11.1. ``_persist_run_metadata`` dumps
            # ``tag_values``, so the seeded key lands in run
            # observability with no further change (Requirement 2.8),
            # and ``llm_inference.render_prompt`` resolves dotted
            # ``{trigger.…}`` placeholders from it unchanged (Req 2.6).
            if "trigger" not in tag_values:
                tag_values["trigger"] = trigger_context

            # Pipeline processing succeeded: consume every
            # directory-resolved source image (staged PNG + original JPEG)
            # NOW — before the Bedrock/LLM/output-binding steps — so the
            # folder drains regardless of downstream outcomes, mirroring
            # the legacy else-branch cleanup
            # (folder-source-image-consumption Reqs 2.1, 2.2, 2.3).
            self._consume_folder_frames(folder_frames)

            # Post-pipeline capture artifact repair (Defect C): a
            # tritonless capture pipeline's broker product is a literal
            # ".jpg" (empty buffer correlation id — only emltriton attaches
            # one); rename empty-basename files to {capture_id}.{ext} so
            # the frame matches run_artifacts.base_output_image_path.
            # Correctly-named (Triton) artifacts are never touched;
            # contained/best-effort (R8.5).
            self._repair_capture_artifacts(execution)

            # Detections into the Run_Metadata (detection-guided-bedrock-
            # inspection Requirements 1.1, 1.5, 1.8, 1.9): merge the
            # run's Detection_List (sorted, Detection_ID'd) and
            # ``detection_count`` AFTER the capture artifacts are
            # repaired and BEFORE the Bedrock processor block, so crops
            # and conditions resolve against it. Built through the SAME
            # run-state cache the bridge pump's injector used, so the
            # handler-visible and post-pipeline lists carry identical
            # Detection_IDs (design Property 1). Contained: a run
            # without a detections record (non-detection model, no
            # record) leaves the metadata unchanged and never fails
            # (Requirement 1.8).
            try:
                detections.merge_detections(
                    tag_values, output_dir, capture_id, graph_document,
                    detections_cache,
                )
            except Exception:  # noqa: BLE001 - contained per 13.7 / R1.8
                logger.exception(
                    "Workflow execution %s could not merge detections into "
                    "the run metadata; the run continues without them",
                    execution_id,
                )

            # Bedrock comparison inference: runs BEFORE the run is
            # finalized and before the gating/output bindings evaluate.
            # The parsed {is_anomalous, confidence} fields merge into
            # the tag values so downstream filters/conditionals/outputs
            # see them; a failure (network, credentials, unparseable
            # response) marks THIS run failed with the node identified
            # and touches nothing else (Requirement 13.7). The
            # ``run_context`` (run metadata, artifact location, graph
            # document, node-status collector) feeds the detection-crop /
            # payload-reference / nested-verdict paths (detection-guided-
            # bedrock-inspection Requirement 7.1); processors whose
            # ``process`` doesn't accept it (injected doubles) are called
            # exactly as today.
            if self._bedrock_processor.bindings(document):
                run_context = RunContext(
                    tag_values=tag_values,
                    output_dir=output_dir,
                    capture_id=capture_id,
                    graph_document=graph_document,
                    node_status=collector,
                )
                try:
                    tag_values = self._bedrock_processor.process(
                        document, tag_values, work_dir,
                        **self._bedrock_process_kwargs(
                            collector, run_context
                        ),
                    )
                except Exception as e:  # noqa: BLE001 - contained per 13.7
                    failing_node_id = getattr(e, "node_id", None)
                    logger.error(
                        "Workflow execution %s failed in Bedrock inference "
                        "(node %s): %s",
                        execution_id,
                        failing_node_id or "unidentified",
                        e,
                    )
                    self._finish_failed(
                        session,
                        execution,
                        error=str(e),
                        failing_node_id=failing_node_id,
                    )
                    self._persist_node_status(
                        session,
                        execution,
                        collector,
                        failing_node_id=failing_node_id,
                        failure_detail=str(e),
                    )
                    return

            # Mid-run node-status snapshot after the Bedrock block
            # (node-execution-timing R2.5). Contained best-effort write.
            self._persist_node_status_snapshot(session, execution, collector)

            # LLM text-generation inference: runs after the Bedrock
            # processor (prompts can reference its merged fields) and
            # before the run is finalized and the gating/output bindings
            # evaluate. Each binding's outcome (generated text, an
            # unresolved-placeholder error, or an API error/timeout)
            # merges under metadata['llm'][nodeId]; a binding failure is
            # recorded, not raised, so remaining bindings and the run's
            # independent nodes continue (vllm-triton-inference
            # Requirements 7.3-7.7).
            if self._llm_processor.bindings(document):
                tag_values = self._llm_processor.process(
                    document, tag_values, work_dir,
                    **self._duration_sink_kwargs(
                        self._llm_processor, collector
                    ),
                )
                # Truthful per-node outcomes for the llm bindings: a
                # recorded {'error': ...} marks THAT node failed in the
                # status map. The run-level COMPLETED decision is
                # unchanged (binding independence, requirement 3.4);
                # successful llm nodes are covered by mark_success_all on
                # the success path.
                self._mark_llm_outcomes(collector, document, tag_values)
                # Mid-run node-status snapshot after the llm outcomes are
                # marked (node-execution-timing R2.5). Contained
                # best-effort write.
                self._persist_node_status_snapshot(
                    session, execution, collector
                )

            # Output bindings run BEFORE the terminal status is
            # finalized so a binding failure surfaces into the run's
            # status, mirroring the Bedrock inference block above. Every
            # binding is still attempted (Requirement 13.7); the handler
            # collects failures and raises OutputBindingError naming the
            # failing node id(s), and only its outcome decides the
            # terminal status. Branch-scoped binding ids already ran in
            # the Bedrock processor's completion path (publish-on-
            # completion) and are excluded here so they never run twice;
            # non-branch bindings keep today's post-run ordering
            # (detection-guided-bedrock-inspection Requirement 5.7) —
            # the exclusion set is empty on every non-concurrent run.
            try:
                self._run_post_run_handler(
                    registration, document, tag_values,
                    detail_sink=(
                        collector.set_detail if collector is not None else None
                    ),
                    duration_sink=self._duration_sink(collector),
                    exclude_node_ids=self._branch_scoped_binding_ids(
                        document
                    ),
                )
                # Mid-run node-status snapshot after the post-run handler
                # returned (node-execution-timing R2.5). Contained
                # best-effort write; it can never raise, so it cannot
                # trip the OutputBindingError handling below.
                self._persist_node_status_snapshot(
                    session, execution, collector
                )
            except OutputBindingError as e:
                failing_node_id = getattr(e, "node_id", None)
                logger.error(
                    "Workflow execution %s failed in an output binding "
                    "(node %s): %s",
                    execution_id,
                    failing_node_id or "unidentified",
                    e,
                )
                self._finish_failed(
                    session,
                    execution,
                    error=str(e),
                    failing_node_id=failing_node_id,
                )
                self._persist_node_status(
                    session,
                    execution,
                    collector,
                    failing_node_id=failing_node_id,
                    failure_detail=str(e),
                )
                # The run's final metadata (llm outcomes included) and
                # the inference-node frames are still persisted on the
                # output-binding-failure path so the artifact directory
                # describes what the run produced (vlm-parity-run-results
                # Requirement 2.2: success OR output-binding failure,
                # before the finally-block removes the work dir).
                self._persist_node_frames(session, execution, document,
                                          work_dir)
                self._persist_run_metadata(execution, tag_values)
                return

            execution.status = EXECUTION_STATUS_COMPLETED
            execution.finished_at = int(time.time())
            session.commit()
            self._persist_node_status(
                session, execution, collector, success=True
            )
            # Persist the inference-node captured frames and the run
            # metadata JSON into the per-run artifact directory BEFORE
            # the finally-block removes the work dir (Defect B/C; the
            # frames sent to Bedrock/VLM previously died with the temp
            # dir — vlm-parity-run-results Requirement 2.2).
            self._persist_node_frames(session, execution, document, work_dir)
            self._persist_run_metadata(execution, tag_values)
            logger.info(
                "Workflow execution %s completed; tags: %s",
                execution_id,
                tag_values,
            )
        except Exception:  # noqa: BLE001 - contained per 13.7
            logger.exception(
                "Workflow execution %s failed unexpectedly", execution_id
            )
            self._mark_failed_best_effort(
                execution_id, "Workflow executor failed unexpectedly; see logs"
            )
        finally:
            # Emit the per-node timing lines BEFORE stopping the log
            # capture so they land inside the run's capture window exactly
            # once on every terminal path (node-execution-timing R4.1,
            # R4.3). Contained: a timing-log failure never affects the run.
            self._emit_timing_logs(collector)
            if log_capture is not None:
                log_capture.stop()
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)
            session.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _begin_log_capture(
        self,
        session,
        execution: WorkflowExecution,
        registration: WorkflowRegistration,
    ) -> Optional[RunLogCapture]:
        """Start per-execution log capture and record ``log_path`` on the row.

        The log path mirrors the run's artifact location so a capture
        workflow's ``run.log`` sits beside its images; non-capture
        workflows still get a per-run logs directory under the same root so
        every started run has a retrievable log (Requirements 2.1, 2.5).

        Entirely best-effort and contained (Requirement 2.6, R8.5): a
        failure to compute the path, persist it, or attach the handler is
        logged and swallowed, and the run proceeds without a captured log
        (returning None)."""
        try:
            log_path = os.path.join(
                _WORKFLOW_CAPTURE_ROOT,
                registration.workflow_id,
                execution.id,
                "run.log",
            )
            capture = RunLogCapture(execution.id, log_path).start()
            try:
                execution.log_path = log_path
                session.commit()
            except Exception:  # noqa: BLE001 - contained per 13.7 / R2.6
                session.rollback()
                logger.exception(
                    "Could not persist log_path for workflow execution %s; "
                    "the run continues with capture active",
                    execution.id,
                )
            return capture
        except Exception:  # noqa: BLE001 - contained per 13.7 / R2.6
            logger.exception(
                "Could not begin run-log capture for workflow execution %s; "
                "the run continues without a captured log",
                execution.id,
            )
            return None

    def _begin_node_status(
        self, name_map: dict, document: Optional[dict] = None
    ) -> Optional[NodeStatusCollector]:
        """Build the run's :class:`NodeStatusCollector`, contained.

        Seeds the collector with the compiled document's
        ``executorBindings`` node ids (llm_inference, mqtt_publish,
        opcua_write, digital_output, bedrock_inference, ...) — these nodes
        have no pipeline element, so without seeding they would never
        appear in the persisted ``node_status_json`` and the run view
        would render them "pending" forever (workflow-output-bindings-fixes
        Defect B). A construction failure is logged and swallowed so the
        run proceeds without per-node status collection rather than
        failing (R8.5)."""
        try:
            binding_node_ids = [
                binding.get("nodeId")
                for binding in (document or {}).get("executorBindings") or []
                if isinstance(binding, dict) and binding.get("nodeId")
            ]
            return NodeStatusCollector(
                name_map, extra_node_ids=binding_node_ids
            )
        except Exception:  # noqa: BLE001 - contained per 13.7 / R8.5
            logger.exception(
                "Could not initialize node-status collection; the run "
                "continues without a per-node status map"
            )
            return None

    @staticmethod
    def _status_sink(collector: Optional[NodeStatusCollector]):
        """The ``run_pipeline`` ``status_sink`` for ``collector``, or None.

        None (no collector) keeps ``run_pipeline`` on its exact pre-feature
        path — the same path every Pipeline_Configuration caller takes
        (R8.1)."""
        return collector.sink if collector is not None else None

    @staticmethod
    def _duration_sink(collector: Optional[NodeStatusCollector]):
        """The binding-processor ``duration_sink`` for ``collector``, or None.

        Maps ``(node_id, elapsed_ms)`` to
        :meth:`NodeStatusCollector.record_invocation_duration`
        (node-execution-timing R1.3, R1.4). None (no collector) makes the
        processors skip timing entirely, keeping the no-collector path
        byte-identical to today."""
        return (
            collector.record_invocation_duration
            if collector is not None
            else None
        )

    def _duration_sink_kwargs(
        self, processor, collector: Optional[NodeStatusCollector]
    ) -> Dict[str, Any]:
        """The ``duration_sink=`` keyword for a directly-held processor's
        ``process(...)`` call, or ``{}``.

        Empty when there is no collector (no timing; the call is
        byte-identical to today) or when the processor's ``process``
        signature does not accept the keyword — injected test doubles
        without it keep working unchanged (node-execution-timing R1.3,
        R5.2)."""
        duration_sink = self._duration_sink(collector)
        if duration_sink is None:
            return {}
        process = getattr(processor, "process", None)
        if process is None or not self._handler_accepts_keyword(
            process, "duration_sink"
        ):
            return {}
        return {"duration_sink": duration_sink}

    def _bedrock_process_kwargs(
        self,
        collector: Optional[NodeStatusCollector],
        run_context: RunContext,
    ) -> Dict[str, Any]:
        """The keyword arguments for the Bedrock processor's
        ``process(...)`` call (detection-guided-bedrock-inspection
        Requirement 7.1).

        Extends :meth:`_duration_sink_kwargs` with ``run_context`` (the
        crop / payload-reference / nested-verdict paths) and
        ``detail_sink`` (branch publishes record their sent-message
        details on the concurrent path). Each keyword is passed only
        when the processor's ``process`` signature accepts it, so
        injected test doubles predating them keep working unchanged —
        the no-keyword call is byte-identical to today."""
        kwargs = self._duration_sink_kwargs(self._bedrock_processor, collector)
        process = getattr(self._bedrock_processor, "process", None)
        if process is None:
            return kwargs
        if self._handler_accepts_keyword(process, "run_context"):
            kwargs["run_context"] = run_context
        if collector is not None and self._handler_accepts_keyword(
            process, "detail_sink"
        ):
            kwargs["detail_sink"] = collector.set_detail
        return kwargs

    def _branch_scoped_binding_ids(self, document: dict) -> set:
        """The binding node ids the post-run handler must skip because
        they already ran in the Bedrock processor's per-branch completion
        path (detection-guided-bedrock-inspection Requirement 5.7).

        Empty — the exact pre-feature post-run call — whenever the
        Bedrock processor has no output seam (sequential legacy path:
        nothing published per-branch, so everything still runs post-run)
        or the document plans no branches. Contained: a planner error
        yields the empty set (running a binding twice is preferable to
        silently dropping it)."""
        if not self._bedrock_branch_publish:
            return set()
        try:
            plans = branching.bedrock_branches(document)
        except Exception:  # noqa: BLE001 - contained per 13.7
            logger.exception(
                "Bedrock branch planning failed; no post-run bindings "
                "are excluded"
            )
            return set()
        excluded: set = set()
        for plan in plans.values():
            excluded.update(plan.binding_ids)
        return excluded

    @staticmethod
    def _load_graph_document(
        registration: WorkflowRegistration,
    ) -> Optional[dict]:
        """The registration's ``workflow.json`` graph document, or None.

        The executor-read source of ``model_inference``'s
        ``detection_sort_order`` (the parameter never reaches the
        compiled document — detection-guided-bedrock-inspection
        Requirement 1.4). Best-effort and contained: any failure yields
        None and the Detection_Sort_Order falls back to the default."""
        try:
            return run_artifacts.read_workflow_graph(
                os.path.join(registration.artifact_path, WORKFLOW_FILE)
            )
        except Exception:  # noqa: BLE001 - best-effort
            logger.debug(
                "Could not load the workflow graph document for %s",
                getattr(registration, "id", None),
                exc_info=True,
            )
            return None

    @staticmethod
    def _detections_injector(
        downstream_ids,
        output_dir: str,
        capture_id: str,
        graph_document: Optional[dict],
        cache: dict,
    ):
        """The bridged run's :class:`python_bridge.DetectionsInjector`,
        or None (detection-guided-bedrock-inspection Requirement 1.10).

        None — the byte-identical pre-feature pump — when no custom node
        is stream-downstream of ``model_inference``. ``cache`` is the
        run-state detections cache the post-pipeline
        ``detections.merge_detections`` call shares (design Property 1).
        """
        if not downstream_ids:
            return None
        return python_bridge.DetectionsInjector(
            downstream_ids,
            output_dir,
            capture_id,
            graph_document=graph_document,
            cache=cache,
        )

    def _preflight_pipeline_factories(
        self,
        document: Dict,
        plugin_dir: str,
        manifest: Optional[Dict],
        artifact_path: str,
    ) -> None:
        """Verify every declared element factory is registered before the
        pipeline parse, raising :class:`MissingPipelineElementError` with
        the originating node identified when one is not.

        Runs inside :func:`workflow_plugin_path` so custom plugin elements
        are already scanned into the registry. The check itself is
        contained (Requirement 13.7): any error computing it skips the
        guard and leaves the pipeline parse as the authority. Only the
        deliberate raise escapes."""
        try:
            factories = rendering.declared_factories(document)
            # Pre-scan the LocalServer-bundled plugin path (eml* elements):
            # gst_pipeline only adds it to the registry when the pipeline
            # manager initializes, which is AFTER this preflight on a fresh
            # process — without it the bundled elements false-positive as
            # missing. GST_PLUGIN_PATH-style ':'-joined; contained.
            bundled_dirs: List[str] = []
            try:
                from utils import utils as _server_utils

                bundled_dirs = [
                    d for d in
                    _server_utils.get_gst_plugins_path().split(":") if d
                ]
            except Exception:  # noqa: BLE001 - path resolution is best-effort
                logger.debug(
                    "Bundled plugin path unavailable for preflight",
                    exc_info=True,
                )
            missing = gst_plugins.missing_factories(
                list(factories), scan_dirs=bundled_dirs
            )
        except Exception:  # noqa: BLE001 - guard must never fail a run itself
            logger.debug(
                "Pipeline factory preflight skipped", exc_info=True
            )
            return
        if not missing:
            return
        provided = gst_plugins.provided_elements(
            plugin_dir, manifest=manifest, artifact_path=artifact_path
        )
        described = ", ".join(
            '"{0}" (node {1})'.format(
                factory, factories.get(factory) or "unidentified"
            )
            for factory in missing
        )
        provided_note = (
            "the run's workflow plugin directories register: {0}".format(
                ", ".join(provided)
            )
            if provided
            else "the run's workflow plugin directories register no elements"
        )
        node_id = next(
            (factories.get(f) for f in missing if factories.get(f)), None
        )
        raise MissingPipelineElementError(
            "Pipeline element(s) not registered with GStreamer: {0}; {1}. "
            "A custom node's declared elementChain factory must exactly "
            "match the element name its built plugin registers.".format(
                described, provided_note
            ),
            node_id=node_id,
        )

    def _persist_node_status(
        self,
        session,
        execution: WorkflowExecution,
        collector: Optional[NodeStatusCollector],
        failing_node_id: Optional[str] = None,
        failure_detail: Optional[str] = None,
        success: bool = False,
    ) -> None:
        """Finalize and persist the terminal ``node_status_json``, contained.

        On a failure path pass ``failing_node_id``/``failure_detail`` to mark
        the mapped node ``failure`` (R3.2); on the clean path pass
        ``success=True`` to mark participating nodes ``success`` (R3.3).
        :meth:`NodeStatusCollector.finalize` then guarantees a fully-terminal
        map (R3.6) — handed the ``failure_detail`` on the failure path so an
        unattributable failure (e.g. a pre-parse ``gst_parse_error``) resolves
        the remaining nodes to ``warning`` with the run error instead of an
        all-``success`` map that would contradict the failed run outcome.
        Entirely best-effort: any error is logged and swallowed so status
        persistence never fails a run (R8.5)."""
        if collector is None:
            return
        try:
            if failing_node_id is not None:
                collector.mark_failure(failing_node_id, failure_detail)
            if success:
                collector.mark_success_all()
            collector.finalize(
                failure_detail=None if success else failure_detail
            )
            execution.node_status_json = collector.to_json()
            session.commit()
        except Exception:  # noqa: BLE001 - contained per 13.7 / R8.5
            try:
                session.rollback()
            except Exception:  # noqa: BLE001 - contained
                pass
            logger.exception(
                "Could not persist node_status_json for workflow execution "
                "%s; the run's terminal status is unaffected",
                execution.id,
            )

    def _persist_node_status_snapshot(
        self,
        session,
        execution: WorkflowExecution,
        collector: Optional[NodeStatusCollector],
    ) -> None:
        """Persist a contained, non-finalizing mid-run snapshot of the
        collector map to ``node_status_json`` (node-execution-timing R2.5).

        Written only from the executor thread at run checkpoints (after the
        pipeline returns, after the Bedrock block, after the LLM outcomes,
        after the post-run handler) so nodes that already reached a terminal
        status expose their ``durationMs`` to the polling node-status
        endpoint while the run is still active. No status is changed and
        nothing is finalized — the terminal :meth:`_persist_node_status`
        remains the authoritative last write. Entirely best-effort: any
        error is logged (debug) and swallowed, session rollback is
        best-effort, and the run proceeds unaffected (R5.4)."""
        if collector is None:
            return
        try:
            execution.node_status_json = collector.to_json()
            session.commit()
        except Exception:  # noqa: BLE001 - contained per R5.4
            try:
                session.rollback()
            except Exception:  # noqa: BLE001 - contained
                pass
            logger.debug(
                "Mid-run node_status_json snapshot failed for workflow "
                "execution %s; the run proceeds unaffected",
                getattr(execution, "id", None),
                exc_info=True,
            )

    def _emit_timing_logs(
        self, collector: Optional[NodeStatusCollector]
    ) -> None:
        """Emit exactly one timing log line per timed node (R4.1, R4.3).

        For each node with a recorded duration, in collector insertion
        order, emits one ``logger.info`` line —
        ``'Node <nodeId> took <formatted>'`` (e.g. ``'Node n3 took
        412 ms'``) — using :func:`format_duration_ms` so the run-log lines
        follow the same formatting rules as the Run_Status_Graph (R4.2).
        Called at the top of :meth:`execute`'s ``finally`` block, before
        ``log_capture.stop()``, so the lines land inside the capture window
        exactly once on every terminal path. Each line is emitted inside
        its own ``try/except`` so one failure cannot suppress the remaining
        lines (R4.4); a ``None`` collector emits nothing."""
        if collector is None:
            return
        try:
            node_ids = list(collector.to_map().keys())
        except Exception:  # noqa: BLE001 - timing is best-effort (R5.4)
            logger.debug(
                "Could not enumerate nodes for timing log emission",
                exc_info=True,
            )
            return
        for node_id in node_ids:
            try:
                duration_ms = collector.duration_ms_of(node_id)
                if duration_ms is None:
                    continue
                logger.info(
                    "Node %s took %s",
                    node_id,
                    format_duration_ms(duration_ms),
                )
            except Exception:  # noqa: BLE001 - contained per R4.4
                logger.debug(
                    "Could not emit the timing log line for node %s; "
                    "continuing with the remaining nodes",
                    node_id,
                    exc_info=True,
                )

    def _mark_llm_outcomes(
        self,
        collector: Optional[NodeStatusCollector],
        document: dict,
        tag_values: dict,
    ) -> None:
        """Mark each failed llm_inference binding's node ``failure`` in the
        run's status map, contained.

        The LlmInferenceProcessor records a binding failure as
        ``metadata['llm'][nodeId] == {'error': reason}`` without raising
        (binding independence, requirement 3.4) — so without this the
        failure would be invisible in ``node_status_json`` (Defect B).
        Successful llm nodes are covered by ``mark_success_all`` on the
        success path. Entirely best-effort: any error is logged and
        swallowed so status marking never fails a run (R8.5)."""
        if collector is None:
            return
        try:
            llm_outcomes = (tag_values or {}).get("llm") or {}
            for binding in self._llm_processor.bindings(document):
                node_id = binding.get("nodeId")
                outcome = llm_outcomes.get(node_id)
                if isinstance(outcome, dict) and outcome.get("error"):
                    collector.mark_failure(node_id, str(outcome["error"]))
        except Exception:  # noqa: BLE001 - contained per 13.7 / R8.5
            logger.exception(
                "Could not mark llm binding outcomes in the node-status "
                "map; the run's terminal status is unaffected"
            )

    #: Filename-unsafe characters in a node id — the same discipline the
    #: portal compiler applies to its capture filenames
    #: (``_UNSAFE_PATH_CHARS`` in workflow_core.compiler.compiler).
    _UNSAFE_NODE_ID_CHARS = re.compile(r"[^A-Za-z0-9_.-]")

    def _persist_node_frames(
        self,
        session,
        execution: WorkflowExecution,
        document: dict,
        work_dir: Optional[str],
    ) -> None:
        """Copy each inference node's captured frames from the per-run
        work dir into the run's artifact directory, contained
        (vlm-parity-run-results Requirements 2.2, 2.4, 4.4).

        For every ``bedrock_inference``/``llm_inference`` executor
        binding carrying ``capturePaths``, each existing resolved frame
        file is copied to ``{output_dir}/{capture_id}.node.
        {sanitized_nodeId}.{port}.jpg`` BEFORE the ``finally`` block
        removes the work dir — so the results view can show what the
        model actually saw. Absent paths/files are skipped silently
        (old packages have no llm capturePaths — Requirement 4.4; the
        compiler emits ``reference: None`` for unfed ports). When at
        least one frame was persisted the run is marked as having
        viewable image results (Requirement 2.4). Entirely best-effort
        in the R8.5 containment style (mirroring
        :meth:`_persist_run_metadata`): any failure is logged and
        swallowed and never changes the run status."""
        try:
            output_dir = getattr(execution, "output_dir", None)
            capture_id = getattr(execution, "capture_id", None)
            if not output_dir or not capture_id or not work_dir:
                return
            copied = 0
            for binding in document.get("executorBindings") or []:
                if binding.get("binding") not in (
                    BINDING_BEDROCK_INFERENCE,
                    BINDING_LLM_INFERENCE,
                ):
                    continue
                node_id = str(binding.get("nodeId") or "")
                safe_node = (
                    self._UNSAFE_NODE_ID_CHARS.sub("_", node_id) or "node"
                )
                for port, path in (binding.get("capturePaths") or {}).items():
                    if not isinstance(path, str) or not path:
                        continue
                    source = path.replace(self._WORK_DIR_TOKEN, work_dir)
                    if not os.path.isfile(source):
                        continue
                    target = os.path.join(
                        output_dir,
                        "{0}.node.{1}.{2}.jpg".format(
                            capture_id, safe_node, port
                        ),
                    )
                    try:
                        shutil.copyfile(source, target)
                        copied += 1
                    except OSError:
                        logger.exception(
                            "Could not persist node frame %s -> %s; "
                            "continuing",
                            source,
                            target,
                        )
            if copied:
                execution.has_image_results = True
                session.commit()
                logger.info(
                    "Persisted %d inference-node frame(s) into %s",
                    copied,
                    output_dir,
                )
        except Exception:  # noqa: BLE001 - contained per 13.7 / R8.5
            logger.exception(
                "Could not persist inference-node frames for workflow "
                "execution %s; the run's terminal status is unaffected",
                execution.id,
            )

    def _persist_run_metadata(
        self, execution: WorkflowExecution, tag_values: dict
    ) -> None:
        """Write the run metadata JSON ``{output_dir}/{capture_id}.json``,
        contained (workflow-output-bindings-fixes Defects B and C).

        Persists the JSON-serializable view of the run's final tag
        values/metadata — notably the ``llm`` section carrying each
        llm_inference node's ``generated_text`` or ``error`` — into the
        per-run artifact directory, so the run's outputs have an on-disk
        destination beside the captured image and ``run.log``. Runs
        without a recorded ``output_dir``/``capture_id`` (non-capture
        documents) are skipped. Entirely best-effort in the R8.5
        containment style: a write failure is logged and swallowed and
        never changes the run status."""
        try:
            output_dir = getattr(execution, "output_dir", None)
            capture_id = getattr(execution, "capture_id", None)
            if not output_dir or not capture_id:
                return
            path = os.path.join(output_dir, "{0}.json".format(capture_id))
            with open(path, "w", encoding="utf-8") as f:
                # default=str keeps the view serializable when tag values
                # carry non-JSON types (best-effort stringification).
                json.dump(tag_values or {}, f, indent=2, default=str)
            logger.info("Persisted run metadata JSON to %s", path)
        except Exception:  # noqa: BLE001 - contained per 13.7 / R8.5
            logger.exception(
                "Could not persist the run metadata JSON for workflow "
                "execution %s; the run's terminal status is unaffected",
                execution.id,
            )

    @staticmethod
    def _repair_capture_artifacts(execution: WorkflowExecution) -> None:
        """Rename empty-basename capture files to ``{capture_id}.{ext}``,
        contained (workflow-output-bindings-fixes Defect C).

        The message broker names capture files ``{c_id}.{ext}`` where
        ``c_id`` is the GStreamer buffer correlation id — attached only by
        ``emltriton`` (via :meth:`_inject_inference_metadata`). A tritonless
        capture pipeline (e.g. filesrc -> ... -> emlcapture) publishes with
        ``c_id = ""`` (``emlcapture.cpp`` SendData default), so the broker
        writes a file literally named ``.jpg``. Repair each such
        empty-stem file — basename exactly ``.{ext}``, one leading dot —
        to ``{capture_id}.{ext}``, aligning the base frame with what
        ``run_artifacts.base_output_image_path`` resolves
        (``{output_dir}/{capture_id}.jpg``). Correctly-named files are
        never touched, so Triton runs (whose broker products already carry
        the capture id) are a no-op. Runs without a recorded
        ``output_dir``/``capture_id`` are skipped. Entirely best-effort in
        the R8.5 containment style: any scan/rename failure is logged and
        swallowed and never changes the run status."""
        try:
            output_dir = getattr(execution, "output_dir", None)
            capture_id = getattr(execution, "capture_id", None)
            if not output_dir or not capture_id:
                return
            try:
                entries = os.listdir(output_dir)
            except OSError:
                return
            for name in entries:
                # Empty stem: exactly one leading dot and a non-empty
                # extension — the broker's `"" + ".ext"` product.
                if not (
                    name.startswith(".")
                    and len(name) > 1
                    and "." not in name[1:]
                ):
                    continue
                source = os.path.join(output_dir, name)
                if not os.path.isfile(source):
                    continue
                target = os.path.join(
                    output_dir, "{0}{1}".format(capture_id, name)
                )
                if os.path.exists(target):
                    logger.warning(
                        "Not repairing capture artifact %s: %s already "
                        "exists",
                        source,
                        target,
                    )
                    continue
                try:
                    os.rename(source, target)
                    logger.info(
                        "Repaired empty-basename capture artifact %s -> %s",
                        source,
                        target,
                    )
                except OSError:
                    logger.exception(
                        "Could not repair capture artifact %s; continuing",
                        source,
                    )
        except Exception:  # noqa: BLE001 - contained per 13.7 / R8.5
            logger.exception(
                "Capture artifact repair failed for workflow execution %s; "
                "the run's terminal status is unaffected",
                execution.id,
            )

    def _run_bridged(
        self, registration, bridge_specs, launch_string, frame_data=None,
        detections_injector=None,
    ) -> dict:
        """Run a launch string containing Custom_Python_Node bridges.

        Builds one subprocess bridge per emlpython element (handler
        paths resolved inside the component's artifacts) and hands the
        rewritten string plus the bridges to the bridged runner —
        ``python_bridge.run_bridged_pipeline`` in production, which
        mirrors GstPipelineManager's watchdog/error/tag patterns while
        pumping frames appsink -> subprocess -> appsrc (Requirement 9.8).

        ``frame_data`` (a Custom Python Produced_Frame) is forwarded to
        the runner so the fed appsrc and the pumped bridges run in the
        same pipeline (custom-python-source Requirement 7.4); ``None``
        (every pre-existing bridged run) keeps the runner invocation
        bit-identical to today (Requirement 11.1).

        ``detections_injector`` (a ``python_bridge.DetectionsInjector``)
        supplies the pump's per-node frame metadata — the run's
        Detection_List for custom nodes stream-downstream of
        ``model_inference`` (detection-guided-bedrock-inspection
        Requirement 1.10). It is forwarded only when non-None AND the
        runner accepts the keyword, so injected runner doubles predating
        it keep working unchanged.
        """
        bridges = python_bridge.build_bridges(
            bridge_specs, registration.artifact_path
        )
        try:
            kwargs: Dict[str, Any] = {}
            if frame_data is not None:
                kwargs["frame_data"] = frame_data
            if detections_injector is not None and self._handler_accepts_keyword(
                self._bridged_pipeline_runner, "detections_injector"
            ):
                kwargs["detections_injector"] = detections_injector
            return self._bridged_pipeline_runner(
                launch_string,
                bridges,
                latency_metrics=_NullLatencyMetrics(),
                **kwargs,
            )
        finally:
            for bridge in bridges:
                bridge.stop()

    # ------------------------------------------------------------------
    # Camera_Binding resolution + Aravis frame feed (aravis-camera-input)
    # ------------------------------------------------------------------

    def _binding_resolution(self, registration: WorkflowRegistration):
        """The registration's latest Camera_Binding resolution, or None.

        A provider failure is logged and falls back to the on-disk
        document — it never takes a run down for documents that don't
        need bindings (design error-handling table)."""
        if self._binding_resolution_provider is None:
            return None
        try:
            return self._binding_resolution_provider(registration.id)
        except Exception:  # noqa: BLE001 - provider isolation
            logger.exception(
                "Binding resolution provider failed for %s; running the "
                "on-disk document",
                registration.id,
            )
            return None

    def _prepare_aravis_frame_feed(self, session, document: dict, resolution):
        """Plan the document's Aravis feed, grab its frame, and point the
        compiled appsrc at the Frame_Feed.

        The grab config is the device's own Image_Source configuration
        for the camera when one is locally configured — a workflow run
        always respects the operator's Aravis camera settings (gain,
        exposure, advanced GenICam controls), exactly like every classic
        preview/capture grab — falling back to the feed's planned
        (binding/rendered) parameters for cameras without a local
        Image_Source.

        Returns the grabbed ``{'data','height','width'}`` frame, or None
        when the document has no Aravis binding points (the pre-feature
        path, Requirement 6.6). Raises :class:`AravisFeedError` (with the
        node id) on planning and grab failures (Requirement 6.5).
        """
        feeds = plan_aravis_feeds(document, resolution)
        if not feeds:
            return None
        feed = feeds[0]
        grab_config = self._camera_config_resolver(session, feed.camera_id)
        if grab_config:
            logger.info(
                "Aravis feed for node %s uses the device Image_Source "
                "configuration for camera '%s'",
                feed.node_id,
                feed.camera_id,
            )
        else:
            grab_config = feed.config
        try:
            frame_data = self._frame_grabber(feed.camera_id, grab_config)
        except Exception as e:  # noqa: BLE001 - attributed to the node
            raise AravisFeedError(
                feed.node_id,
                "frame grab from Aravis camera '{0}' failed: {1}".format(
                    feed.camera_id, e
                ),
            )
        if not isinstance(frame_data, dict):
            raise AravisFeedError(
                feed.node_id,
                "Aravis camera '{0}' returned no frame".format(feed.camera_id),
            )
        self._point_appsrc_at_frame_feed(document, feed, frame_data)
        logger.info(
            "Aravis frame feed planned for node %s: camera '%s' (%dx%d)",
            feed.node_id,
            feed.camera_id,
            frame_data.get("width") or 0,
            frame_data.get("height") or 0,
        )
        return frame_data

    def _prepare_python_source_feed(
        self, document: dict, registration, trigger_context: Dict[str, Any]
    ):
        """Plan the document's Frame_Producer, run it, and point the
        compiled appsrc at the Frame_Feed (custom-python-source
        Requirements 7.1-7.3).

        Returns ``(frame_data, producer_metadata)``: the Produced_Frame
        as ``{'data','width','height','format'}`` plus the producer's
        metadata keyed by the node id (empty when the producer returned
        none — Requirement 6.7), or ``(None, None)`` when the document
        plans zero sources — the exact pre-feature path (Requirement
        7.3). Raises :class:`PythonSourceError` /
        :class:`~workflow_engine.python_bridge.CustomPythonNodeError`
        carrying the node id on planning and production failures
        (Requirements 6.4, 6.5, 7.6, 8.5).
        """
        feeds = plan_python_sources(document)
        if not feeds:
            return None, None
        feed = feeds[0]
        bridge = python_bridge.build_producer_bridge(
            feed, registration.artifact_path
        )
        try:
            data, width, height, frame_format, metadata = (
                bridge.produce_frame(
                    trigger_context, feed.allowed_uri_prefixes
                )
            )
        finally:
            bridge.stop()
        # Every source the producer fetched through dda_frames lands in
        # the run log via RunLogCapture (Requirement 5.4).
        fetched_sources = list(
            getattr(bridge, "fetched_sources", None) or ()
        )
        if fetched_sources:
            logger.info(
                "Custom Python source %s fetched %d source(s): %s",
                feed.node_id,
                len(fetched_sources),
                ", ".join(fetched_sources),
            )
        frame_data = {
            "data": data,
            "width": width,
            "height": height,
            "format": frame_format,
        }
        self._point_appsrc_at_frame_feed(
            document, feed, frame_data, error_cls=PythonSourceError
        )
        logger.info(
            "Custom Python source frame produced for node %s: %dx%d %s",
            feed.node_id,
            width,
            height,
            frame_format,
        )
        return frame_data, ({feed.node_id: metadata} if metadata else {})

    @staticmethod
    def _point_appsrc_at_frame_feed(
        document: dict, feed, frame_data: dict, error_cls=AravisFeedError
    ) -> None:
        """Aim ``GstPipelineManager``'s Frame_Feed at the node's appsrc.

        ``run_pipeline`` locates the feed's appsrc by the element name
        ``appsrc`` and derives its caps from the launch string's ``caps=``
        clause plus the frame's width/height — exactly the classic
        Camera-type execution model. The compiled document names the
        element ``appsrc_{nodeId}`` and renders no caps, so the planned
        feed's element (unique per the single-Frame_Feed contract) is
        renamed and given base caps derived from the fed frame.

        ``feed`` is duck-typed over ``node_id`` (an Aravis feed or a
        Python source feed); ``error_cls`` names the feed family's error
        type — both carry the ``node_id`` the executor reads for failure
        attribution.
        """
        caps = WorkflowExecutor._frame_caps(frame_data)
        for segment in document.get("segments", []):
            elements = segment.get("elements", [])
            for index, element in enumerate(elements):
                if (
                    element.get("nodeId") == feed.node_id
                    and element.get("factory") == "appsrc"
                ):
                    args = element.setdefault("args", {})
                    args["name"] = "appsrc"
                    args["caps"] = caps
                    if caps.startswith("video/x-bayer"):
                        # A Bayer-mosaic camera frame must be demosaiced
                        # before the compiled chain's videoconvert (which
                        # cannot consume video/x-bayer). Mirrors the classic
                        # Image_Source conversion (`... video/x-bayer ! `
                        # `bayer2rgb ! ...`), attributed to the source node
                        # so a bayer2rgb failure names it.
                        elements.insert(index + 1, {
                            "nodeId": element.get("nodeId"),
                            "factory": "bayer2rgb",
                            "args": {},
                        })
                    return
        raise error_cls(
            feed.node_id,
            "compiled document renders no appsrc element for the node",
        )

    @staticmethod
    def _frame_caps(frame_data: dict) -> str:
        """Base appsrc caps for a fed frame; ``run_pipeline`` appends
        the frame's width/height.

        A frame declaring an explicit ``format`` (every Custom Python
        Produced_Frame — custom-python-source Requirement 7.2) names
        exactly that Pixel_Format. A frame tagged ``pixel_format`` by the
        camera grab (camera_manager.gst_pixel_format) names the camera's
        ACTUAL format: ``bayer:<pattern>`` becomes ``video/x-bayer`` caps
        (the caller inserts the bayer2rgb demosaic), a raw name becomes
        ``video/x-raw`` — a Bayer mosaic is 1 byte/pixel like Mono8, so
        the size guess below cannot distinguish them and used to mislabel
        Bayer cameras GRAY8 (garbage frames downstream). Untagged frames
        keep the byte-per-pixel derivation, defaulting to GRAY8 —
        bit-identical to the pre-feature behavior (Requirement 11.1)."""
        explicit_format = frame_data.get("format")
        if explicit_format:
            return "video/x-raw,format={0}".format(explicit_format)
        pixel_format = frame_data.get("pixel_format")
        if isinstance(pixel_format, str) and pixel_format:
            if pixel_format.startswith("bayer:"):
                return "video/x-bayer,format={0}".format(
                    pixel_format.split(":", 1)[1])
            return "video/x-raw,format={0}".format(pixel_format)
        formats = {1: "GRAY8", 3: "RGB", 4: "RGBA"}
        width = frame_data.get("width") or 0
        height = frame_data.get("height") or 0
        data = frame_data.get("data")
        pixel_format = "GRAY8"
        if width and height and data is not None:
            pixels = width * height
            if len(data) % pixels == 0:
                pixel_format = formats.get(len(data) // pixels, "GRAY8")
        return "video/x-raw,format={0}".format(pixel_format)

    #: Placeholder the compiler leaves in bedrock_inference capture
    #: paths for the executor to resolve per run.
    _WORK_DIR_TOKEN = "{work_dir}"

    @classmethod
    def _needs_work_dir(cls, document: dict) -> bool:
        """True when the document references the {work_dir} placeholder
        (element args or bedrock_inference capturePaths)."""
        for segment in document.get("segments", []):
            for element in segment.get("elements", []):
                for value in (element.get("args") or {}).values():
                    if isinstance(value, str) and cls._WORK_DIR_TOKEN in value:
                        return True
        for binding in document.get("executorBindings") or []:
            paths = binding.get("capturePaths") or {}
            for value in paths.values():
                if isinstance(value, str) and cls._WORK_DIR_TOKEN in value:
                    return True
        return False

    def _prepare_work_dir(self, document: dict) -> Optional[str]:
        """Create the per-run working directory and resolve {work_dir}
        into the document's element args, or None when unused."""
        if not self._needs_work_dir(document):
            return None
        work_dir = tempfile.mkdtemp(prefix="workflow-run-")
        substitutions = rendering.resolve_placeholder(
            document, "work_dir", work_dir
        )
        logger.info(
            "Resolved {work_dir} -> %s (%d element substitution(s))",
            work_dir,
            substitutions,
        )
        return work_dir

    @staticmethod
    def _preflight(registration: Optional[WorkflowRegistration]) -> Optional[str]:
        """Reason the run cannot start, or None when it can."""
        if registration is None:
            return "Workflow registration no longer exists"
        if registration.status != STATUS_REGISTERED:
            # Invalid registrations are rejected at trigger time too; this
            # covers artifacts invalidated between trigger and dispatch.
            return (
                f"Workflow registration '{registration.id}' is "
                f"'{registration.status}' and cannot be run"
            )
        return None

    @staticmethod
    def _load_manifest(registration: WorkflowRegistration) -> Optional[dict]:
        """The artifact set's manifest.json, or None when unreadable."""
        path = os.path.join(registration.artifact_path, MANIFEST_FILE)
        try:
            with open(path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            return None
        return manifest if isinstance(manifest, dict) else None

    @staticmethod
    def _load_compiled_document(registration: WorkflowRegistration):
        """(document, None) or (None, error message)."""
        path = os.path.join(registration.artifact_path, COMPILED_PIPELINE_FILE)
        try:
            with open(path, "r", encoding="utf-8") as f:
                document = json.load(f)
        except (OSError, ValueError) as e:
            return None, f"Cannot load {COMPILED_PIPELINE_FILE}: {e}"
        if not isinstance(document, dict) or not isinstance(
            document.get("segments"), list
        ):
            return None, (
                f"Malformed {COMPILED_PIPELINE_FILE}: missing 'segments' list"
            )
        return document, None

    @staticmethod
    def _finish_failed(
        session,
        execution: WorkflowExecution,
        error: str,
        failing_node_id: Optional[str] = None,
    ) -> None:
        """Record the failure on the execution row — the record the
        existing /workflows/executions status endpoint reports
        (Requirement 9.7)."""
        execution.status = EXECUTION_STATUS_FAILED
        execution.failing_node_id = failing_node_id
        execution.error = error
        execution.finished_at = int(time.time())
        session.commit()

    def _mark_failed_best_effort(self, execution_id: str, error: str) -> None:
        """Last-resort failure marking with a fresh session (the run's own
        session may be the thing that broke)."""
        try:
            session = self._session_factory()
            try:
                execution = session.get(WorkflowExecution, execution_id)
                if execution is not None and execution.status in (
                    EXECUTION_STATUS_PENDING,
                    EXECUTION_STATUS_RUNNING,
                ):
                    self._finish_failed(session, execution, error=error)
            finally:
                session.close()
        except Exception:  # noqa: BLE001 - truly nothing more to do
            logger.exception(
                "Could not record failure for workflow execution %s",
                execution_id,
            )

    def _run_post_run_handler(
        self,
        registration: WorkflowRegistration,
        document: dict,
        tag_values: dict,
        detail_sink: Optional[Callable] = None,
        duration_sink: Optional[Callable] = None,
        exclude_node_ids: Optional[set] = None,
    ) -> None:
        """Invoke the output-binding hook (task 12.4).

        An :class:`OutputBindingError` (one or more output bindings
        failed) PROPAGATES to the caller so the run's terminal status
        reflects the failed binding(s). Any OTHER unexpected handler
        exception stays contained (Requirement 13.7) so an unrelated
        handler bug cannot crash the run.

        ``detail_sink`` (the per-run collector's ``set_detail``, when a
        collector exists) is threaded to handlers that accept it so output
        bindings can record their sent-message / skipped-outcome details into
        node_status_json (output-node-sent-message feature).
        ``duration_sink`` (the collector's ``record_invocation_duration``)
        is threaded the same way so output bindings can report their
        invocation durations (node-execution-timing R1.3, R1.4). The
        generic ``PostRunHandler`` signature is ``(registration, document,
        tag_values) -> None``; a handler that accepts neither keyword
        keeps working unchanged (backward compatibility).

        ``exclude_node_ids`` (detection-guided-bedrock-inspection
        Requirement 5.7) names the branch-scoped bindings that already
        ran in the Bedrock processor's per-branch completion path. When
        non-empty — only ever on the concurrent publish-on-completion
        path, whose activation requires a handler with
        ``process_subset`` — the handler runs the REMAINING bindings
        through ``process_subset`` (same code paths, ``executorBindings``
        order preserved) so branch outputs never publish twice.
        None/empty keeps the exact pre-feature handler invocation."""
        if self._post_run_handler is None:
            return
        try:
            handler_subset = (
                getattr(self._post_run_handler, "process_subset", None)
                if exclude_node_ids
                else None
            )
            if callable(handler_subset):
                remaining = [
                    binding.get("nodeId")
                    for binding in (document.get("executorBindings") or [])
                    if binding.get("nodeId") not in exclude_node_ids
                ]
                kwargs: Dict[str, Any] = {}
                if detail_sink is not None:
                    kwargs["detail_sink"] = detail_sink
                if duration_sink is not None:
                    kwargs["duration_sink"] = duration_sink
                handler_subset(document, tag_values, remaining, **kwargs)
            else:
                self._invoke_post_run_handler(
                    registration, document, tag_values, detail_sink,
                    duration_sink,
                )
        except OutputBindingError:
            raise
        except Exception:  # noqa: BLE001 - contained per 13.7
            logger.exception(
                "Workflow post-run handler failed for %s v%s",
                registration.workflow_id,
                registration.version,
            )

    def _invoke_post_run_handler(
        self,
        registration: WorkflowRegistration,
        document: dict,
        tag_values: dict,
        detail_sink: Optional[Callable],
        duration_sink: Optional[Callable] = None,
    ) -> None:
        """Call the post-run handler, passing ``detail_sink`` and
        ``duration_sink`` only when the handler accepts them (backward
        compatibility).

        The generic ``PostRunHandler`` signature is ``(registration,
        document, tag_values) -> None``; :class:`OutputBindingProcessor`
        additionally accepts ``detail_sink`` and ``duration_sink``
        keywords. Handlers that do not (plain lambdas, other
        implementations) are invoked without the keywords they don't
        accept and keep working unchanged."""
        handler = self._post_run_handler
        kwargs: Dict[str, Any] = {}
        if detail_sink is not None and self._handler_accepts_keyword(
            handler, "detail_sink"
        ):
            kwargs["detail_sink"] = detail_sink
        if duration_sink is not None and self._handler_accepts_keyword(
            handler, "duration_sink"
        ):
            kwargs["duration_sink"] = duration_sink
        if kwargs:
            handler(registration, document, tag_values, **kwargs)
        else:
            handler(registration, document, tag_values)

    @staticmethod
    def _handler_accepts_keyword(handler: Callable, name: str) -> bool:
        """True when ``handler`` accepts the keyword argument ``name``.

        Inspects the callable's signature (``__call__`` for instances);
        tolerates un-inspectable callables (e.g. some builtins) by returning
        False so the original positional call is used."""
        try:
            target = handler
            if not (inspect.isfunction(handler) or inspect.ismethod(handler)):
                # Callable instance: inspect its __call__.
                call = getattr(handler, "__call__", None)
                if call is not None:
                    target = call
            signature = inspect.signature(target)
        except (TypeError, ValueError):
            return False
        parameters = signature.parameters
        if name in parameters:
            return True
        # A **kwargs-accepting handler can take the keyword too.
        return any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in parameters.values()
        )

    @classmethod
    def _handler_accepts_detail_sink(cls, handler: Callable) -> bool:
        """Backward-compatible alias for the pre-generalization shim name:
        True when ``handler`` accepts a ``detail_sink`` keyword argument."""
        return cls._handler_accepts_keyword(handler, "detail_sink")


def register_workflow_executor(
    session_factory: Optional[Callable] = None,
    pipeline_manager_factory: Optional[Callable] = None,
    post_run_handler: Optional[PostRunHandler] = None,
    binding_resolution_provider: Optional[Callable] = None,
) -> WorkflowExecutor:
    """Create a WorkflowExecutor and register it as THE executor hook.

    Called from ``runtime.start_workflow_engine`` so triggered runs stop
    staying pending once the engine is up.
    """
    instance = WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=pipeline_manager_factory,
        post_run_handler=post_run_handler,
        binding_resolution_provider=binding_resolution_provider,
    )
    executor_hook.set_executor(instance.execute)
    logger.info("WorkflowExecutor registered as the workflow executor")
    return instance
