"""Triton model staging inside the sandbox container.

The compiled document the sandbox executes is produced with
``simulation=True``, which maps every model_inference node to a
pass-through stub chain (``capsfilter ! identity name=sim_inference_<id>``
— see workflow_core.catalog.nodes.MODEL_INFERENCE). On device
architectures the compiler instead emits::

    emltriton model-repo=/aws_dda/dda_triton/triton_model_repo \
              server-path=/opt/tritonserver model=<modelName>

The sandbox image ships the emltriton plugin and CPU Triton at exactly
those paths, but its model repository starts EMPTY — nothing would
serve the model. This module closes that gap:

1. ``STAGED_MODELS`` (set by the RunSandbox container override from the
   staging manifest workflow_testing.py records) lists
   ``[{nodeId, modelName, s3Key}, ...]`` — model artifact zips the
   portal copied into the artifacts bucket under the run's prefix.
2. Each zip is downloaded and unpacked into the Triton model
   repository in the layout Triton expects (below).
3. The document's ``sim_inference_<nodeId>`` identity stubs are
   rewritten into real emltriton elements whose ``model=<modelName>``
   matches the staged repository entry name exactly, so the pipeline
   performs real CPU inference instead of injecting the simulated
   outcome.

Artifact layout (inspected against live registry components — see
backend/functions/workflow_model_staging.py for the registry side):
the greengrass model component zip is NOT a ready Triton repository.
It contains the raw runtime artifact plus its manifest, e.g.::

    model.onnx        (runtime artifact)
    manifest.json     ({"runtime": "onnx", "model_graph": {...},
                        "dataset": {...}, "preprocessing": {...}})

On device, ``src/backend/dda_triton/model_convertor.py`` converts that
into a three-entry python-backend Triton repository, which this module
replicates::

    <repo>/base_<name>/config.pbtxt            python backend, raw model
    <repo>/base_<name>/1/model.py              (lfv_model_template.py)
    <repo>/base_<name>/1/inference_runtimes.py
    <repo>/base_<name>/1/<zip contents>        (model.onnx, manifest.json)
    <repo>/marshal_<name>/config.pbtxt         python backend, marshaling
    <repo>/marshal_<name>/1/model.py           (marshal_for_capture_template.py)
    <repo>/<name>/config.pbtxt                 ensemble base -> marshal
    <repo>/<name>/1/ensemble_model             (placeholder Triton requires)

``<name>`` is the workflow node's ``modelName`` — the exact string the
compiler puts into ``model=<modelName>`` on device builds — so the
ensemble entry emltriton requests always matches the repository
directory name (the device uses the Greengrass component name instead;
staging under the node's model name is what reconciles the two).

The python-backend templates ship with the sandbox image under
``DDA_TRITON_RESOURCES`` (staged at image build from
``src/backend/dda_triton/resources_for_copy/`` — see
test-sandbox/README.md), like the DDA plugin ``.so`` set. When they are
absent the staging fails with a clear per-node error instead of a
Triton crash.

CPU-only normalization (no GPU exists on Fargate):

- Generated config.pbtxt files declare no ``instance_group`` — the
  Triton python backend then defaults to KIND_CPU, matching the
  device-side converter's output.
- If a zip instead contains a prebuilt Triton repository (any
  ``config.pbtxt`` present), every config's ``instance_group`` is
  rewritten to ``KIND_CPU`` (``KIND_GPU``/``KIND_AUTO`` replaced,
  ``gpus: [...]`` device lists dropped) so Triton never tries to place
  the model on a GPU.
- The staged ``manifest.json`` gets ``"device": "cpu"`` so the ONNX
  runner selects the CPUExecutionProvider explicitly rather than
  probing for CUDA.
"""

import json
import logging
import os
import re
import shutil
import zipfile
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("sandbox-harness")

#: Where emltriton's model-repo argument points (workflow_core
#: DEFAULT_CONTEXT_VALUES); overridable for tests.
DEFAULT_MODEL_REPO = "/aws_dda/dda_triton/triton_model_repo"
#: Where the CPU Triton binary lives in the sandbox image (the
#: server-path argument emltriton starts the server from).
DEFAULT_SERVER_PATH = "/opt/tritonserver"
#: Image location of the python-backend templates staged at build time.
DEFAULT_TEMPLATE_DIR = "/opt/dda/dda_triton_resources"

#: Repository version directory (the device converter uses the
#: component's major version; the sandbox repo is per-run, so "1").
MODEL_VERSION = "1"

#: Error code recorded on per-node model staging failures.
MODEL_STAGING_FAILED = "MODEL_STAGING_FAILED"

#: Template files the DDA conversion copies into the repository.
BASE_TEMPLATE = "lfv_model_template.py"
RUNTIMES_TEMPLATE = "inference_runtimes.py"
MARSHAL_TEMPLATE = "marshal_for_capture_template.py"
ENSEMBLE_PLACEHOLDER = "ensemble_model"

_SIM_INFERENCE_PREFIX = "sim_inference_"


class ModelStagingError(Exception):
    """A model artifact could not be staged; carries the failing
    node/model so the harness records a per-node error (12.10)."""

    def __init__(self, node_id: Optional[str], model_name: Optional[str],
                 message: str):
        super().__init__(message)
        self.node_id = node_id
        self.model_name = model_name


def model_repo_dir() -> str:
    return os.environ.get("TRITON_MODEL_REPO", DEFAULT_MODEL_REPO)


def server_path() -> str:
    return os.environ.get("TRITON_SERVER_PATH", DEFAULT_SERVER_PATH)


def template_dir() -> str:
    return os.environ.get("DDA_TRITON_RESOURCES", DEFAULT_TEMPLATE_DIR)


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------

def parse_staged_models(raw: Optional[str]) -> List[Dict[str, str]]:
    """The staging manifest from the STAGED_MODELS env JSON.

    Entries missing nodeId/modelName/s3Key are dropped (defensive: the
    start endpoint always writes complete entries). Absent or malformed
    input yields [] — the run then behaves like a pre-staging run and
    falls back to the simulated-inference stubs.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("Malformed STAGED_MODELS %r; no models staged", raw)
        return []
    if not isinstance(parsed, list):
        logger.warning("STAGED_MODELS is not a list; no models staged")
        return []
    entries = []
    for entry in parsed:
        if (isinstance(entry, dict) and entry.get("nodeId")
                and entry.get("modelName") and entry.get("s3Key")):
            entries.append({
                "nodeId": str(entry["nodeId"]),
                "modelName": str(entry["modelName"]),
                "s3Key": str(entry["s3Key"]),
            })
        else:
            logger.warning("Dropping malformed STAGED_MODELS entry %r", entry)
    return entries


# ---------------------------------------------------------------------------
# CPU-only config normalization
# ---------------------------------------------------------------------------

_KIND_PATTERN = re.compile(r"\bKIND_(?:GPU|AUTO|MODEL)\b")
_GPUS_LINE_PATTERN = re.compile(r"^\s*gpus\s*:\s*\[[^\]]*\]\s*,?\s*$",
                                re.MULTILINE)


def force_cpu_instance_groups(config_text: str) -> str:
    """Rewrite a config.pbtxt for CPU-only execution: GPU/auto instance
    kinds become KIND_CPU and per-group ``gpus: [...]`` device lists are
    dropped (they are invalid alongside KIND_CPU). Configs without
    instance groups pass through unchanged — the python backend then
    defaults to CPU."""
    rewritten = _KIND_PATTERN.sub("KIND_CPU", config_text)
    return _GPUS_LINE_PATTERN.sub("", rewritten)


# ---------------------------------------------------------------------------
# config.pbtxt generation (mirrors src/backend/dda_triton/model_convertor.py)
# ---------------------------------------------------------------------------

def _tensor(name: str, data_type: str, dims: List[int]) -> str:
    return ("  {{\n    name: \"{0}\"\n    data_type: {1}\n"
            "    dims: [{2}]\n  }}").format(
                name, data_type, ", ".join(str(d) for d in dims))


def _tensor_block(field: str, tensors: List[str]) -> str:
    return "{0} [\n{1}\n]".format(field, ",\n".join(tensors))


def resolve_base_input_shape(manifest: Dict) -> List[int]:
    """The "input" tensor dims — the exact rule model_convertor applies:
    ONNX-runtime models always use a dynamic [-1, -1, -1] input (the
    python model resizes internally per the manifest); DLR models with
    pixel-level classes use the fixed [H, W, 3] training size; anything
    else is dynamic."""
    runtime = str(manifest.get("runtime", "dlr")).lower()
    if runtime == "onnx":
        return [-1, -1, -1]
    pixel_level = (manifest.get("model_graph") or {}).get("pixel_level_classes") or {}
    if pixel_level.get("names"):
        dataset = manifest.get("dataset") or {}
        return [dataset["image_height"], dataset["image_width"], 3]
    return [-1, -1, -1]


def base_config_pbtxt(model_name: str, input_shape: List[int]) -> str:
    """``base_<name>`` python-backend config (raw model inference)."""
    inputs = [_tensor("input", "TYPE_UINT8", input_shape)]
    outputs = [
        _tensor("output", "TYPE_UINT8", [1]),
        _tensor("output_confidence", "TYPE_FP32", [1]),
        _tensor("output_score", "TYPE_FP32", [1]),
        _tensor("mask", "TYPE_UINT8", input_shape),
        _tensor("anomalies", "TYPE_UINT8", [-1]),
    ]
    return "\n".join([
        'name: "base_{0}"'.format(model_name),
        _tensor_block("input", inputs),
        _tensor_block("output", outputs),
        'backend: "python"',
        "",
    ])


def marshal_config_pbtxt(model_name: str, input_shape: List[int]) -> str:
    """``marshal_<name>`` python-backend config (capture marshaling)."""
    inputs = [
        _tensor("input", "TYPE_UINT8", input_shape),
        _tensor("inference_output", "TYPE_UINT8", [1]),
        _tensor("inference_mask", "TYPE_UINT8", input_shape),
        _tensor("inference_confidence", "TYPE_FP32", [1]),
        _tensor("inference_score", "TYPE_FP32", [1]),
        _tensor("inference_anomalies", "TYPE_UINT8", [-1]),
        _tensor("metadata", "TYPE_UINT8", [-1]),
    ]
    outputs = [
        _tensor("output", "TYPE_UINT8", [-1]),
        _tensor("mask", "TYPE_UINT8", [-1]),
        _tensor("overlay", "TYPE_UINT8", [-1]),
        _tensor("output_anomalous", "TYPE_UINT8", [1]),
        _tensor("output_confidence", "TYPE_FP32", [1]),
    ]
    return "\n".join([
        'name: "marshal_{0}"'.format(model_name),
        _tensor_block("input", inputs),
        _tensor_block("output", outputs),
        'backend: "python"',
        "",
    ])


def _map_entries(field: str, mapping: List[Tuple[str, str]]) -> str:
    return "\n".join(
        "      {0} {{\n        key: \"{1}\"\n        value: \"{2}\"\n      }}"
        .format(field, key, value)
        for key, value in mapping
    )


def ensemble_config_pbtxt(model_name: str, input_shape: List[int]) -> str:
    """``<name>`` ensemble config chaining base -> marshal, exposing the
    tensor set emltriton consumes (output_anomalous / output_confidence /
    output_overlay / output_mask / output_capture)."""
    inputs = [
        _tensor("input", "TYPE_UINT8", input_shape),
        _tensor("METADATA", "TYPE_UINT8", [-1]),
    ]
    outputs = [
        _tensor("output_capture", "TYPE_UINT8", [-1]),
        _tensor("output_mask", "TYPE_UINT8", [-1]),
        _tensor("output_overlay", "TYPE_UINT8", [-1]),
        _tensor("output_anomalous", "TYPE_UINT8", [1]),
        _tensor("output_confidence", "TYPE_FP32", [1]),
    ]
    step_base = "\n".join([
        "    {",
        '      model_name: "base_{0}"'.format(model_name),
        "      model_version: -1",
        _map_entries("input_map", [("input", "input")]),
        _map_entries("output_map", [
            ("output", "inference_output"),
            ("mask", "inference_mask"),
            ("anomalies", "inference_anomalies"),
            ("output_score", "inference_score"),
            ("output_confidence", "inference_confidence"),
        ]),
        "    }",
    ])
    step_marshal = "\n".join([
        "    {",
        '      model_name: "marshal_{0}"'.format(model_name),
        "      model_version: -1",
        _map_entries("input_map", [
            ("input", "input"),
            ("inference_output", "inference_output"),
            ("inference_mask", "inference_mask"),
            ("inference_anomalies", "inference_anomalies"),
            ("inference_confidence", "inference_confidence"),
            ("inference_score", "inference_score"),
            ("metadata", "METADATA"),
        ]),
        _map_entries("output_map", [
            ("output", "output_capture"),
            ("mask", "output_mask"),
            ("overlay", "output_overlay"),
            ("output_anomalous", "output_anomalous"),
            ("output_confidence", "output_confidence"),
        ]),
        "    }",
    ])
    return "\n".join([
        'name: "{0}"'.format(model_name),
        'platform: "ensemble"',
        _tensor_block("input", inputs),
        _tensor_block("output", outputs),
        "ensemble_scheduling {",
        "  step [",
        step_base + ",",
        step_marshal,
        "  ]",
        "}",
        "",
    ])


# ---------------------------------------------------------------------------
# Zip extraction + repository staging
# ---------------------------------------------------------------------------

def _extract_zip(zip_path: str, extract_dir: str, node_id: Optional[str],
                 model_name: str) -> None:
    try:
        with zipfile.ZipFile(zip_path) as archive:
            bad = archive.testzip()
            if bad is not None:
                raise zipfile.BadZipFile("corrupt member: " + bad)
            archive.extractall(extract_dir)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as error:
        raise ModelStagingError(
            node_id, model_name,
            "Model {0}: the staged artifact is not a readable zip "
            "({1})".format(model_name, error))


def _configs_in(extract_dir: str) -> List[str]:
    """Relative directories (from extract_dir) holding a config.pbtxt."""
    found = []
    for root, _dirs, files in os.walk(extract_dir):
        if "config.pbtxt" in files:
            found.append(os.path.relpath(root, extract_dir))
    return sorted(found)


def _find_manifest_dir(extract_dir: str) -> Optional[str]:
    """The directory containing manifest.json — at the zip root or one
    directory down (some component zips wrap their contents)."""
    if os.path.isfile(os.path.join(extract_dir, "manifest.json")):
        return extract_dir
    entries = [e for e in sorted(os.listdir(extract_dir))
               if os.path.isdir(os.path.join(extract_dir, e))]
    for entry in entries:
        candidate = os.path.join(extract_dir, entry)
        if os.path.isfile(os.path.join(candidate, "manifest.json")):
            return candidate
    return None


def _copy_templates_into(version_dir: str, source_dir: str,
                         main_template: str) -> None:
    """``main_template`` becomes the version dir's model.py; every other
    template file / package directory (inference_runtimes.py, the
    lyra_* app packages the templates import) is copied alongside so
    the Triton python-backend stub — which forwards no PYTHONPATH —
    resolves them from the model directory (the templates put their own
    directory on sys.path)."""
    shutil.copy(os.path.join(source_dir, main_template),
                os.path.join(version_dir, "model.py"))
    for entry in sorted(os.listdir(source_dir)):
        if entry in (BASE_TEMPLATE, MARSHAL_TEMPLATE, ENSEMBLE_PLACEHOLDER):
            continue
        if entry == "__pycache__":
            continue
        source = os.path.join(source_dir, entry)
        target = os.path.join(version_dir, entry)
        if os.path.isdir(source):
            shutil.copytree(source, target, dirs_exist_ok=True)
        elif not os.path.exists(target):
            shutil.copy(source, target)


def _replace_model_dir(path: str) -> None:
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path)


def _stage_dda_component(model_dir: str, model_name: str, repo_dir: str,
                         templates: str, node_id: Optional[str]) -> None:
    """Convert an unpacked DDA greengrass model component (manifest.json
    + runtime artifact) into the three-entry Triton repository — the
    sandbox-side equivalent of model_convertor.convert_to_triton_structure
    with ``model_name`` = the workflow node's modelName."""
    required = (BASE_TEMPLATE, RUNTIMES_TEMPLATE, MARSHAL_TEMPLATE,
                ENSEMBLE_PLACEHOLDER)
    missing = [name for name in required
               if not os.path.isfile(os.path.join(templates, name))]
    if missing:
        raise ModelStagingError(
            node_id, model_name,
            "Model {0}: the sandbox image was built without the DDA "
            "Triton conversion resources ({1} missing from {2}); see "
            "test-sandbox/README.md".format(
                model_name, ", ".join(missing), templates))

    manifest_path = os.path.join(model_dir, "manifest.json")
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError) as error:
        raise ModelStagingError(
            node_id, model_name,
            "Model {0}: manifest.json is unreadable ({1})".format(
                model_name, error))
    input_shape = resolve_base_input_shape(manifest)

    # base_<name>: config + python model + runtime abstraction + the
    # unpacked model files (the device symlinks; the sandbox copies).
    base_dir = os.path.join(repo_dir, "base_" + model_name)
    _replace_model_dir(base_dir)
    base_version = os.path.join(base_dir, MODEL_VERSION)
    os.makedirs(base_version)
    with open(os.path.join(base_dir, "config.pbtxt"), "w",
              encoding="utf-8") as handle:
        handle.write(base_config_pbtxt(model_name, input_shape))
    _copy_templates_into(base_version, templates, BASE_TEMPLATE)
    for entry in sorted(os.listdir(model_dir)):
        source = os.path.join(model_dir, entry)
        target = os.path.join(base_version, entry)
        if os.path.isdir(source):
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy(source, target)
    # Pin the ONNX runner to the CPU execution provider (no GPU exists
    # on Fargate; the KIND_CPU analog for the python runtime).
    manifest["device"] = "cpu"
    with open(os.path.join(base_version, "manifest.json"), "w",
              encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)

    # marshal_<name>: config + marshaling python model.
    marshal_dir = os.path.join(repo_dir, "marshal_" + model_name)
    _replace_model_dir(marshal_dir)
    marshal_version = os.path.join(marshal_dir, MODEL_VERSION)
    os.makedirs(marshal_version)
    with open(os.path.join(marshal_dir, "config.pbtxt"), "w",
              encoding="utf-8") as handle:
        handle.write(marshal_config_pbtxt(model_name, input_shape))
    _copy_templates_into(marshal_version, templates, MARSHAL_TEMPLATE)

    # <name>: the ensemble emltriton requests by model=<modelName>.
    ensemble_dir = os.path.join(repo_dir, model_name)
    _replace_model_dir(ensemble_dir)
    ensemble_version = os.path.join(ensemble_dir, MODEL_VERSION)
    os.makedirs(ensemble_version)
    with open(os.path.join(ensemble_dir, "config.pbtxt"), "w",
              encoding="utf-8") as handle:
        handle.write(ensemble_config_pbtxt(model_name, input_shape))
    shutil.copy(os.path.join(templates, ENSEMBLE_PLACEHOLDER),
                os.path.join(ensemble_version, ENSEMBLE_PLACEHOLDER))


_CONFIG_NAME_PATTERN = re.compile(r'^(\s*name\s*:\s*)"([^"]*)"',
                                  re.MULTILINE)


def _stage_prebuilt_repo(extract_dir: str, config_dirs: List[str],
                         model_name: str, repo_dir: str,
                         node_id: Optional[str]) -> None:
    """Stage a zip that already contains a Triton model repository
    (one directory per model, each holding config.pbtxt): copy the
    model directories in, rewrite every config for CPU execution, and
    normalize the entry name to ``model_name`` so it matches the
    ``model=<modelName>`` argument the compiler emits.

    Normalization rule: if a copied entry is already named
    ``model_name`` nothing changes; otherwise, when the repository has
    exactly one entry (no ensemble/base split), it is renamed —
    directory and config ``name:`` — to ``model_name``. Multi-entry
    repositories without a matching entry fail with a clear error
    (renaming one of several cross-referencing entries would break the
    ensemble graph).
    """
    entry_names = []
    for relative in config_dirs:
        # Model directories are the immediate parents of config.pbtxt;
        # nested layouts (wrapper folder) keep only the leaf name.
        entry_names.append(os.path.basename(relative.rstrip("/")) or relative)

    if model_name not in entry_names and len(config_dirs) != 1:
        raise ModelStagingError(
            node_id, model_name,
            "Model {0}: the artifact contains a multi-entry Triton "
            "repository ({1}) with no entry named {0}; the emltriton "
            "element requests model={0}".format(
                model_name, ", ".join(sorted(entry_names))))

    for relative, entry_name in zip(config_dirs, entry_names):
        source = os.path.join(extract_dir, relative)
        target_name = entry_name
        rename = False
        if entry_name != model_name and len(config_dirs) == 1:
            target_name = model_name
            rename = True
        target = os.path.join(repo_dir, target_name)
        _replace_model_dir(target)
        shutil.copytree(source, target, dirs_exist_ok=True)
        config_path = os.path.join(target, "config.pbtxt")
        with open(config_path, encoding="utf-8") as handle:
            config_text = handle.read()
        config_text = force_cpu_instance_groups(config_text)
        if rename:
            config_text = _CONFIG_NAME_PATTERN.sub(
                lambda match: '{0}"{1}"'.format(match.group(1), model_name),
                config_text, count=1)
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write(config_text)


def stage_model_zip(zip_path: str, model_name: str, repo_dir: str,
                    templates: Optional[str] = None,
                    node_id: Optional[str] = None,
                    workdir: Optional[str] = None) -> None:
    """Unpack one staged model zip into the Triton model repository.

    Two layouts are recognized (see the module docstring):

    - a prebuilt Triton repository (any ``config.pbtxt`` inside): copied
      with CPU normalization and name reconciliation;
    - the DDA greengrass model component layout (``manifest.json`` +
      runtime artifact): converted like the device-side model_convertor.

    Raises :class:`ModelStagingError` on a corrupt/unrecognizable
    artifact.
    """
    templates = templates or template_dir()
    extract_dir = os.path.join(workdir or os.path.dirname(zip_path),
                               "unpack-" + model_name)
    if os.path.isdir(extract_dir):
        shutil.rmtree(extract_dir)
    os.makedirs(extract_dir)
    _extract_zip(zip_path, extract_dir, node_id, model_name)

    config_dirs = _configs_in(extract_dir)
    if config_dirs:
        _stage_prebuilt_repo(extract_dir, config_dirs, model_name, repo_dir,
                             node_id)
        return
    model_dir = _find_manifest_dir(extract_dir)
    if model_dir is None:
        raise ModelStagingError(
            node_id, model_name,
            "Model {0}: unrecognized artifact layout (neither a Triton "
            "model repository nor a DDA model component with "
            "manifest.json)".format(model_name))
    _stage_dda_component(model_dir, model_name, repo_dir, templates, node_id)


def download_and_stage(s3, bucket: str, entries: List[Dict[str, str]],
                       repo_dir: str, workdir: str,
                       templates: Optional[str] = None) -> Dict[str, str]:
    """Download every staged model zip and populate the Triton model
    repository. Returns ``{nodeId: modelName}`` for the staged nodes.

    A missing or corrupt artifact raises :class:`ModelStagingError`
    identifying the model_inference node (per-node error + exit 1
    semantics, Requirement 12.10).
    """
    os.makedirs(repo_dir, exist_ok=True)
    os.makedirs(workdir, exist_ok=True)
    staged_by_node: Dict[str, str] = {}
    staged_models = set()
    for entry in entries:
        node_id = entry["nodeId"]
        model_name = entry["modelName"]
        if model_name in staged_models:
            staged_by_node[node_id] = model_name
            continue
        zip_path = os.path.join(workdir, model_name + ".zip")
        try:
            s3.download_file(bucket, entry["s3Key"], zip_path)
        except Exception as error:
            raise ModelStagingError(
                node_id, model_name,
                "Model {0}: the staged artifact s3://{1}/{2} could not be "
                "downloaded ({3})".format(model_name, bucket,
                                          entry["s3Key"], error))
        stage_model_zip(zip_path, model_name, repo_dir,
                        templates=templates, node_id=node_id,
                        workdir=workdir)
        staged_models.add(model_name)
        staged_by_node[node_id] = model_name
        logger.info("Staged model '%s' into %s (node %s)",
                    model_name, repo_dir, node_id)
    return staged_by_node


# ---------------------------------------------------------------------------
# Compiled-document reconciliation
# ---------------------------------------------------------------------------

def realize_inference_elements(document: Dict,
                               staged_by_node: Dict[str, str],
                               repo_dir: Optional[str] = None,
                               server: Optional[str] = None) -> List[str]:
    """Rewrite staged nodes' simulation stubs into real emltriton
    elements.

    The simulation compiler maps model_inference to ``identity
    name=sim_inference_<nodeId>`` (the modelName parameter is dropped
    from the compiled document); for every staged node that stub becomes
    the emltriton element device builds carry — ``model-repo`` /
    ``server-path`` pointing at the sandbox paths and ``model`` set to
    the staged repository entry name, which equals the node's modelName.
    Nodes without a staged model keep their stub (and the simulated
    outcome injection). Returns the rewritten node ids in document
    order.
    """
    repo = repo_dir or model_repo_dir()
    srv = server or server_path()
    realized: List[str] = []
    for segment in document.get("segments", []):
        for element in segment.get("elements", []):
            name = element.get("args", {}).get("name")
            node_id = element.get("nodeId")
            if (element.get("factory") == "identity"
                    and isinstance(name, str)
                    and name.startswith(_SIM_INFERENCE_PREFIX)
                    and node_id in staged_by_node):
                element["factory"] = "emltriton"
                element["args"] = {
                    "model-repo": repo,
                    "server-path": srv,
                    "model": staged_by_node[node_id],
                }
                realized.append(node_id)
    return realized
