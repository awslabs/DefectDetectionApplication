#!/usr/bin/env python3
"""Export-only SageMaker entry point for imported detector checkpoints.

Spec: .kiro/specs/detector-checkpoint-import (Requirements 5 and 6,
design.md sections 2-3). The portal starts this as a SageMaker training job
with EnableNetworkIsolation=True on the Export_Image: the container has no
network and no AWS credentials. That is the whole point -- loading an
ultralytics checkpoint unpickles it, i.e. runs code chosen by whoever produced
the file, and this is the only place in the product where that is allowed to
happen. A malicious checkpoint can at worst write a bad artifact, which the
portal re-validates as untrusted before packaging (Requirement 8).

Contract (environment, set by the portal's build_conversion_job_request):
  DETECTION_ARCH        'yolo' | 'rf_detr'
  NETWORK_INPUT         square export size S (multiple of 32)
  EXPECTED_NUM_CLASSES  C the portal recorded from its own probe
  EXPECTED_SHA256       sha256 of the one checkpoint in the input channel
  ONNX_OPSET            default 17 (must be <= FLEET_MAX_OPSET)
  RFDETR_SIZE           optional nano|small|medium|large (inferred otherwise)

Input:  /opt/ml/input/data/checkpoint/<exactly one file>
Output: /opt/ml/model/{model.onnx, training_metadata.json} -- exactly these two
        (SageMaker tars the directory into model.tar.gz)
Failure: one line "FATAL: ..." in /opt/ml/output/failure, which SageMaker
        surfaces as the job's FailureReason (first 1024 characters).

Everything is verified here before anything is written:
  * the checkpoint is what the portal probed (sha256), and a detector: the
    ultralytics task/model/head, or an RF-DETR size whose architecture matches
    the state_dict key-for-key and shape-for-shape;
  * the class count equals the record's;
  * the graph honours the Device_Output_Contract (YOLO: one [1, 4+C, N] output,
    no embedded NMS, no one-to-one head; RF-DETR: [1,Q,4] + [1,Q,C+1]),
    float32 I/O, static [1, 3, S, S] input;
  * IR version <= 9 and default-domain opset <= 19, only the default operator
    domain, no external data (the fleet floor: onnxruntime 1.16.3 on JP5 and
    the CPU / x86 images);
  * the graph loads and runs on onnxruntime 1.16.3 itself (separate venv) and
    on the exporter's current onnxruntime, and on both its outputs match the
    source model's on the same inputs (the Parity_Check; RF-DETR compares
    emitted detections, not raw query slots -- see PARITY_TOLERANCE).

Portal-trained artifacts are the shape contract: the metadata carries the keys
packaging.package_trained_detection_component reads, so a converted import is
packaged by exactly the code that packages a portal-trained detector.
"""
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# ultralytics reads these once, when ultralytics.utils is first imported: never
# pip-install (Requirement 5.7) and never probe the network.
os.environ.setdefault("YOLO_AUTOINSTALL", "False")
os.environ.setdefault("YOLO_OFFLINE", "True")

INPUT_DIR = Path(os.environ.get("CHECKPOINT_INPUT_DIR", "/opt/ml/input/data/checkpoint"))
MODEL_DIR = Path(os.environ.get("SM_MODEL_DIR", "/opt/ml/model"))
FAILURE_FILE = Path(os.environ.get("FAILURE_FILE", "/opt/ml/output/failure"))
# On the job's attached volume (the trainers' WORK lives under the same root).
WORK = Path(os.environ.get("EXPORT_WORK_DIR", "/opt/ml/input/work/export"))
ORT_FLOOR_PYTHON = os.environ.get("ORT_FLOOR_PYTHON", "/opt/ort-floor/bin/python")

ARCHES = ("yolo", "rf_detr")
# Fleet_Floor_Runtime: the oldest onnxruntime among the default packaging
# targets (JP5 GPU build, CPU / x86 images).
FLEET_FLOOR_ORT = "1.16.3"
FLEET_MAX_OPSET = 19
FLEET_MAX_IR = 9
# IR version written when an exporter stamps a newer one (ultralytics 8.4
# clamps to 10; onnxslim re-serialises with the installed onnx's IR). 8 is the
# IR that opset-17 graphs are defined against; the fleet-floor load proves it.
TARGET_IR = 8
DEFAULT_OPSET = 17
ALLOWED_OP_DOMAINS = ("ai.onnx",)
INPUT_STEP = 32
INPUT_BOUNDS = {"yolo": (320, 2048), "rf_detr": (224, 1120)}

# ultralytics: the only model class / head classes the device's
# YoloDetectionPostProcessor decodes correctly (spike: docs/detector-checkpoint-import-spike.md).
# YOLO26 checkpoints carry `Detect`; YOLOv10's NMS-free `v10Detect` also has a
# trained one-to-many branch, which `nms=None` exports as [1, 4+C, N] (spike:
# same detections as its own one-to-one head on the bundled images).
YOLO_MODEL_CLASS = "ultralytics.nn.tasks.DetectionModel"
YOLO_ACCEPTED_HEADS = ("ultralytics.nn.modules.head.Detect", "ultralytics.nn.modules.head.v10Detect")
# Raw one-to-many output, no embedded NMS, fp32, static shapes. On ultralytics
# >= 8.4 `nms=None` is "external NMS" (the default); `nms=False` would select
# YOLO26's NMS-free one-to-one head, (N, 300, 6), which the device mis-decodes.
YOLO_EXPORT_ARGS = {
    "format": "onnx",
    "dynamic": False,
    "simplify": True,
    "nms": None,
    "batch": 1,
    "device": "cpu",
    "verbose": False,
}

# RF-DETR: the Apache-2.0 detection sizes (the trainers' RFDETR_SIZES). The
# PML-licensed XLarge / 2XLarge, the legacy Base / LargeDeprecated and the
# segmentation variants are never candidates.
RFDETR_SIZE_CLASSES = {
    "nano": "RFDETRNano",
    "small": "RFDETRSmall",
    "medium": "RFDETRMedium",
    "large": "RFDETRLarge",
}
# Keys rfdetr itself treats as intentional schema buffers, never weights.
RFDETR_IGNORED_KEYS = ("_kp_active_mask",)
# Recognised by the checkpoint's `model_name` (rfdetr.detr's own class map)
# only to give a precise reason; a checkpoint without a name must still match
# an Apache-2.0 size key-for-key and shape-for-shape to convert.
RFDETR_PML_MODEL_NAMES = ("RFDETRXLarge", "RFDETR2XLarge", "RFDETRSegXLarge", "RFDETRSeg2XLarge")
RFDETR_NON_DETECTION_PREFIXES = ("RFDETRSeg", "RFDETRKeypoint")
RFDETR_LEGACY_MODEL_NAMES = ("RFDETRBase", "RFDETRLargeDeprecated")

# Parity tolerances (spike-measured; see docs/detector-checkpoint-import-spike.md).
#   yolo    -- element-wise over the whole [1, 4+C, N] tensor: the one-to-many
#              graph has no data-dependent selection, so every anchor is
#              comparable. Boxes in network pixels [0, S], scores sigmoided.
#   rf_detr -- the two-stage query selection (encoder top-k) can swap near-tied,
#              low-confidence proposals between runtimes, which reorders or
#              replaces a few tail queries (spike: 15 of 300 slots on the
#              published Nano + bus.jpg under onnxruntime 1.16.3, all scoring
#              < 0.09, while every detection >= 0.25 matched to 1e-5). So:
#              (1) every query/class pair the device could emit at a threshold
#              >= score_floor must match one-to-one (same class, nearest box)
#              within box/score tolerance, with no extra pairs on either side;
#              (2) at least min_slot_agreement of the query slots must have a
#              one-to-one partner (same top class, box and top probability
#              within tolerance), which keeps the check meaningful on inputs
#              with no confident detection at all -- a broken export agrees on
#              ~0% of slots. Boxes normalised cxcywh [0, 1]; scores are
#              sigmoid probabilities of the C class slots (the trailing
#              background slot is never emitted).
# Worst measured over the spike fixtures (both runtimes, both inputs): YOLO box
# 0.0063 px / score 3.2e-6; RF-DETR detections box 8.1e-6 / score 2.0e-5, slot
# agreement >= 0.95.
PARITY_TOLERANCE = {
    "yolo": {"box_atol": 0.1, "score_atol": 1e-3},
    "rf_detr": {"box_atol": 1e-3, "score_atol": 5e-3, "score_floor": 0.25,
                "min_slot_agreement": 0.75},
}
PARITY_SEED = 20260925
FAILURE_MAX_CHARS = 1024


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    print(message, flush=True)


def fatal(message: str) -> None:
    """Abort the job with a user-facing reason (the `FATAL:` convention the
    trainers use; the portal shows it verbatim as the failure reason)."""
    raise SystemExit(f"FATAL: {message}")


def write_failure(reason: str, path: Optional[Path] = None) -> None:
    """Write the single-line failure reason SageMaker surfaces (Req 5.6)."""
    path = FAILURE_FILE if path is None else path
    line = " ".join(str(reason).split())
    if not line.startswith("FATAL:"):
        line = f"FATAL: {line}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(line[:FAILURE_MAX_CHARS])
    except OSError as e:  # never mask the original failure
        log(f"WARN: could not write {path}: {e}")


def distribution_version(name: str) -> str:
    """The installed version of a distribution (rfdetr has no __version__)."""
    from importlib import metadata

    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unknown"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_config(env: Dict[str, str]) -> SimpleNamespace:
    """The job's configuration from the environment; FATAL on anything the
    portal should never have sent (pure)."""
    arch = (env.get("DETECTION_ARCH") or "").strip().lower()
    if arch not in ARCHES:
        fatal(f"DETECTION_ARCH must be one of {', '.join(ARCHES)}; got {arch!r}")

    def as_int(name: str, default: Optional[str] = None) -> int:
        raw = env.get(name, default)
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            fatal(f"{name} must be an integer; got {raw!r}")
        raise AssertionError  # unreachable

    network_input = as_int("NETWORK_INPUT")
    low, high = INPUT_BOUNDS[arch]
    if network_input % INPUT_STEP or not low <= network_input <= high:
        fatal(f"NETWORK_INPUT must be a multiple of {INPUT_STEP} in [{low}, {high}] "
              f"for {arch}; got {network_input}")
    num_classes = as_int("EXPECTED_NUM_CLASSES")
    if num_classes < 1:
        fatal(f"EXPECTED_NUM_CLASSES must be >= 1; got {num_classes}")
    opset = as_int("ONNX_OPSET", str(DEFAULT_OPSET))
    if not 11 <= opset <= FLEET_MAX_OPSET:
        fatal(f"ONNX_OPSET must be in [11, {FLEET_MAX_OPSET}] (the fleet floor is "
              f"onnxruntime {FLEET_FLOOR_ORT}); got {opset}")
    expected_sha = (env.get("EXPECTED_SHA256") or "").strip().lower()
    if len(expected_sha) != 64 or any(c not in "0123456789abcdef" for c in expected_sha):
        fatal("EXPECTED_SHA256 must be the 64-character hex sha256 of the checkpoint")
    size = (env.get("RFDETR_SIZE") or "").strip().lower() or None
    if size is not None and (arch != "rf_detr" or size not in RFDETR_SIZE_CLASSES):
        fatal(f"RFDETR_SIZE must be one of {', '.join(RFDETR_SIZE_CLASSES)} "
              f"(and only for rf_detr); got {size!r}")
    return SimpleNamespace(arch=arch, network_input=network_input,
                           num_classes=num_classes, opset=opset,
                           expected_sha256=expected_sha, rfdetr_size=size)


def single_input_file(directory: Path) -> Path:
    """Exactly one regular file in the input channel (Req 5.3)."""
    if not directory.is_dir():
        fatal(f"input channel {directory} is missing")
    entries = sorted(directory.rglob("*"))
    files = [p for p in entries if p.is_file() and not p.is_symlink()]
    others = [p for p in entries if not p.is_dir() and p not in files]
    if others:
        fatal(f"input channel holds non-regular entries: {[p.name for p in others]}")
    if len(files) != 1:
        fatal(f"input channel must hold exactly one checkpoint; found {len(files)}: "
              f"{[p.name for p in files][:5]}")
    return files[0]


def disable_autoupdate() -> Dict[str, bool]:
    """ultralytics AutoUpdate off before import (the trainers' switch set)."""
    applied = {"env": True, "constant": False, "settings": False}
    os.environ["YOLO_AUTOINSTALL"] = "False"
    try:
        import ultralytics.utils as ul_utils

        ul_utils.AUTOINSTALL = False
        try:
            import ultralytics.utils.checks as ul_checks

            ul_checks.AUTOINSTALL = False
        except Exception:  # noqa: BLE001
            pass
        applied["constant"] = True
    except Exception as e:  # noqa: BLE001
        log(f"WARN: could not force ultralytics AUTOINSTALL off: {e}")
    try:
        from ultralytics import settings

        settings.update(autoinstall=False)
        applied["settings"] = True
    except Exception:  # noqa: BLE001 - not every version has the key
        pass
    return applied


# ---------------------------------------------------------------------------
# ONNX structure (the exporter-side twin of the portal's torch-free validator)
# ---------------------------------------------------------------------------

def onnx_summary(path: Path) -> Dict[str, Any]:
    """I/O, IR, opsets, operator domains and external-data use of a graph."""
    import onnx

    model = onnx.load(str(path), load_external_data=False)

    def dims(value_info) -> List[Any]:
        return [d.dim_value if d.HasField("dim_value") else (d.dim_param or None)
                for d in value_info.type.tensor_type.shape.dim]

    def dtype(value_info) -> str:
        return onnx.TensorProto.DataType.Name(value_info.type.tensor_type.elem_type)

    def walk_nodes(graph):
        for node in graph.node:
            yield node
            for attr in node.attribute:
                if attr.type == onnx.AttributeProto.GRAPH:
                    yield from walk_nodes(attr.g)
                elif attr.type == onnx.AttributeProto.GRAPHS:
                    for g in attr.graphs:
                        yield from walk_nodes(g)

    initializers = {t.name for t in model.graph.initializer}
    external = [t.name for t in model.graph.initializer
                if t.data_location == onnx.TensorProto.EXTERNAL]
    return {
        "ir_version": int(model.ir_version),
        "opsets": {(o.domain or "ai.onnx"): int(o.version) for o in model.opset_import},
        "domains": sorted({(n.domain or "ai.onnx") for n in walk_nodes(model.graph)}),
        "functions": len(model.functions),
        "external_data": external,
        "inputs": [(i.name, dims(i), dtype(i)) for i in model.graph.input
                   if i.name not in initializers],
        "outputs": [(o.name, dims(o), dtype(o)) for o in model.graph.output],
        "metadata_props": {p.key: p.value for p in model.metadata_props},
    }


def normalize_ir_version(path: Path) -> Tuple[int, int]:
    """Clamp the model's IR version to TARGET_IR when an exporter stamped a
    newer one the fleet floor cannot load. Returns (before, after). Only the
    header field changes; the fleet-floor load afterwards is the proof that
    nothing in the graph needed the newer IR."""
    import onnx

    model = onnx.load(str(path), load_external_data=False)
    before = int(model.ir_version)
    if before > FLEET_MAX_IR:
        model.ir_version = TARGET_IR
        onnx.save(model, str(path))
    return before, int(model.ir_version)


def verify_fleet_structure(summary: Dict[str, Any]) -> None:
    """IR / opset / domains / external data against the fleet floor (pure)."""
    if summary["ir_version"] > FLEET_MAX_IR:
        fatal(f"ONNX IR version {summary['ir_version']} exceeds {FLEET_MAX_IR}, the maximum "
              f"onnxruntime {FLEET_FLOOR_ORT} (JP5, CPU images) loads")
    default_opset = summary["opsets"].get("ai.onnx")
    if default_opset is None or default_opset > FLEET_MAX_OPSET:
        fatal(f"ONNX default-domain opset {default_opset} exceeds {FLEET_MAX_OPSET}, the maximum "
              f"onnxruntime {FLEET_FLOOR_ORT} supports")
    bad_domains = [d for d in summary["domains"] if d not in ALLOWED_OP_DOMAINS]
    if bad_domains:
        fatal(f"ONNX graph uses non-default operator domains {bad_domains}")
    if summary["functions"]:
        fatal(f"ONNX graph defines {summary['functions']} local functions; not supported")
    if summary["external_data"]:
        fatal(f"ONNX graph stores tensors as external data: {summary['external_data'][:3]}")


def _static_int_dims(shape: Sequence[Any]) -> bool:
    return all(isinstance(d, int) and not isinstance(d, bool) and d > 0 for d in shape)


def verify_input_contract(summary: Dict[str, Any], network_input: int) -> List[int]:
    """Exactly one float32 input, static [1, 3, S, S] (pure)."""
    inputs = summary["inputs"]
    if len(inputs) != 1:
        fatal(f"expected exactly 1 ONNX input, got {len(inputs)}: {inputs}")
    name, shape, dtype = inputs[0]
    expected = [1, 3, network_input, network_input]
    if dtype != "FLOAT":
        fatal(f"ONNX input {name} is {dtype}; the device feeds float32 (FLOAT)")
    if list(shape) != expected:
        fatal(f"ONNX input {name} is {list(shape)}; expected static {expected}")
    return expected


def verify_yolo_contract(summary: Dict[str, Any], network_input: int,
                         num_classes: int) -> Dict[str, Any]:
    """The YoloDetectionPostProcessor contract (pure): one float32 output
    [1, 4+C, N] (or [1, N, 4+C]) with 4 + C < N. Anything else -- an embedded
    NMS [1, K, 6], YOLO26's one-to-one head, a segmentation output with mask
    coefficients, extra outputs the decoder would silently ignore -- is FATAL."""
    input_shape = verify_input_contract(summary, network_input)
    outputs = summary["outputs"]
    quoted = ", ".join(f"{n}={s}:{t}" for n, s, t in outputs)
    if len(outputs) != 1:
        fatal(f"expected exactly 1 ONNX output [1, {num_classes + 4}, N] (the device decodes "
              f"output[0] only); got {len(outputs)}: [{quoted}]")
    name, shape, dtype = outputs[0]
    if dtype != "FLOAT":
        fatal(f"ONNX output {name} is {dtype}; expected float32 (FLOAT)")
    if len(shape) != 3 or not _static_int_dims(shape) or shape[0] != 1:
        fatal(f"ONNX output must be a static rank-3 batch-1 tensor; got [{quoted}]")
    channels = num_classes + 4
    a, b = int(shape[1]), int(shape[2])
    if a == channels and b > channels:
        anchors, layout = b, "channels_first"
    elif b == channels and a > channels:
        anchors, layout = a, "channels_last"
    else:
        fatal(f"ONNX output {name} {list(shape)} is not [1, {channels}, N] or [1, N, {channels}] "
              f"with N > {channels} (num_classes={num_classes} + 4 box channels). An embedded-NMS "
              f"or one-to-one export looks like [1, K, 6]; a segmentation head adds 32 mask "
              f"channels")
    return {"input_shape": input_shape, "output_shape": [int(d) for d in shape],
            "anchors": anchors, "layout": layout}


# ---------------------------------------------------------------------------
# Fleet floor + parity
# ---------------------------------------------------------------------------

_FLOOR_SCRIPT = r"""
import json, sys
import numpy as np
import onnxruntime as ort
model, inputs_npz, out_npz = sys.argv[1:4]
opts = ort.SessionOptions()
opts.intra_op_num_threads = 2
sess = ort.InferenceSession(model, sess_options=opts, providers=["CPUExecutionProvider"])
feeds = np.load(inputs_npz)
name = sess.get_inputs()[0].name
results = {}
report = {"ort": ort.__version__, "outputs": [o.name for o in sess.get_outputs()], "runs": {}}
for key in feeds.files:
    outs = sess.run(None, {name: feeds[key]})
    for idx, arr in enumerate(outs):
        results[f"{key}__{idx}"] = arr
    report["runs"][key] = [{"shape": list(a.shape), "dtype": str(a.dtype),
                            "finite": bool(np.isfinite(a).all())} for a in outs]
np.savez(out_npz, **results)
print(json.dumps(report))
"""


def run_on_fleet_floor(onnx_path: Path, inputs: Dict[str, Any]) -> SimpleNamespace:
    """Load and run the graph on onnxruntime 1.16.3 (the /opt/ort-floor venv)
    and return its outputs per input. FATAL if it cannot load, run, or emits
    NaN / Inf (Req 6.6)."""
    import numpy as np

    WORK.mkdir(parents=True, exist_ok=True)
    feeds = WORK / "floor_inputs.npz"
    results = WORK / "floor_outputs.npz"
    np.savez(feeds, **{k: np.ascontiguousarray(v, dtype=np.float32) for k, v in inputs.items()})
    proc = subprocess.run(
        [ORT_FLOOR_PYTHON, "-c", _FLOOR_SCRIPT, str(onnx_path), str(feeds), str(results)],
        capture_output=True, text=True, check=False, timeout=900)
    if proc.returncode != 0:
        tail = " ".join((proc.stderr or proc.stdout).strip().splitlines()[-3:])
        fatal(f"onnxruntime {FLEET_FLOOR_ORT} (the JP5 / CPU fleet floor) could not load or run "
              f"the graph: {tail}")
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    if report.get("ort") != FLEET_FLOOR_ORT:
        fatal(f"fleet-floor venv runs onnxruntime {report.get('ort')}, expected {FLEET_FLOOR_ORT}")
    for key, runs in report["runs"].items():
        for idx, run in enumerate(runs):
            if not run["finite"]:
                fatal(f"onnxruntime {FLEET_FLOOR_ORT} output {idx} contains NaN/Inf on the "
                      f"{key} parity input")
    loaded = np.load(results)
    outputs = {key: [loaded[f"{key}__{i}"] for i in range(len(runs))]
               for key, runs in report["runs"].items()}
    return SimpleNamespace(ort=report["ort"], output_names=report["outputs"], outputs=outputs)


def _bundled_image() -> Optional[Path]:
    """A real photo baked into the image: ultralytics ships assets/bus.jpg."""
    override = os.environ.get("PARITY_IMAGE")
    if override:
        return Path(override)
    try:
        from ultralytics.utils import ASSETS

        candidate = Path(ASSETS) / "bus.jpg"
        return candidate if candidate.is_file() else None
    except Exception:  # noqa: BLE001
        return None


IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)


def parity_inputs(arch: str, size: int) -> Dict[str, Any]:
    """Deterministic parity inputs: a seeded synthetic tensor and a real image
    preprocessed the way the device does (YOLO: letterbox, pad 114, /255;
    RF-DETR: square resize, /255, ImageNet mean/std)."""
    import numpy as np

    rng = np.random.default_rng(PARITY_SEED)
    synthetic = rng.random((1, 3, size, size), dtype=np.float32)
    if arch == "rf_detr":
        mean = np.array(IMAGENET_MEAN, dtype=np.float32).reshape(1, 3, 1, 1)
        std = np.array(IMAGENET_STD, dtype=np.float32).reshape(1, 3, 1, 1)
        synthetic = (synthetic - mean) / std
    inputs = {"synthetic": synthetic.astype(np.float32)}
    image_path = _bundled_image()
    if image_path is not None:
        import cv2

        bgr = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if bgr is not None:
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            if arch == "yolo":
                h, w = rgb.shape[:2]
                ratio = min(size / w, size / h)
                rw, rh = int(round(w * ratio)), int(round(h * ratio))
                canvas = np.full((size, size, 3), 114, dtype=np.uint8)
                top, left = (size - rh) // 2, (size - rw) // 2
                canvas[top:top + rh, left:left + rw] = cv2.resize(
                    rgb, (rw, rh), interpolation=cv2.INTER_AREA)
                tensor = canvas.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
            else:
                resized = cv2.resize(rgb, (size, size), interpolation=cv2.INTER_AREA)
                tensor = resized.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
                mean = np.array(IMAGENET_MEAN, dtype=np.float32).reshape(1, 3, 1, 1)
                std = np.array(IMAGENET_STD, dtype=np.float32).reshape(1, 3, 1, 1)
                tensor = (tensor - mean) / std
            inputs["image"] = np.ascontiguousarray(tensor, dtype=np.float32)
    return inputs


def run_on_export_runtime(onnx_path: Path, inputs: Dict[str, Any]) -> SimpleNamespace:
    """Run the graph on the exporter environment's own (current) onnxruntime:
    the export-faithfulness half of the Parity_Check, closest to the JP6 /
    JP7 runtimes (1.20.1 / 1.23.2)."""
    import numpy as np
    import onnxruntime as ort

    try:
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        sess = ort.InferenceSession(str(onnx_path), sess_options=opts,
                                    providers=["CPUExecutionProvider"])
        name = sess.get_inputs()[0].name
        outputs = {key: [np.asarray(a) for a in sess.run(None, {name: np.ascontiguousarray(
            batch, dtype=np.float32)})] for key, batch in inputs.items()}
    except Exception as e:  # noqa: BLE001
        fatal(f"onnxruntime {ort.__version__} could not load or run the exported graph: "
              f"{type(e).__name__}: {e}")
    for key, arrays in outputs.items():
        for idx, arr in enumerate(arrays):
            if not np.isfinite(arr).all():
                fatal(f"onnxruntime {ort.__version__} output {idx} contains NaN/Inf on the "
                      f"{key} parity input")
    return SimpleNamespace(ort=ort.__version__, output_names=[o.name for o in sess.get_outputs()],
                           outputs=outputs)


def compare_yolo_parity(reference: Dict[str, List[Any]], got: Dict[str, List[Any]],
                        num_classes: int, tolerance: Dict[str, float],
                        runtime: str) -> Dict[str, Any]:
    """Element-wise over every anchor; box and score channels separately
    (different units). Pure numpy; FATAL beyond tolerance."""
    import numpy as np

    report: Dict[str, Any] = {}
    for key, ref_list in reference.items():
        ref = np.asarray(ref_list[0], dtype=np.float64)
        out = np.asarray(got[key][0], dtype=np.float64)
        if ref.shape != out.shape:
            fatal(f"parity: {runtime} {key} output shape {list(out.shape)} differs from the "
                  f"source model's {list(ref.shape)}")
        channel_axis = 1 if ref.shape[1] == num_classes + 4 else 2
        box = np.take(ref, range(4), axis=channel_axis) - np.take(out, range(4), axis=channel_axis)
        cls_idx = range(4, 4 + num_classes)
        score = np.take(ref, cls_idx, axis=channel_axis) - np.take(out, cls_idx, axis=channel_axis)
        entry = {"box_max_abs": float(np.abs(box).max()),
                 "score_max_abs": float(np.abs(score).max()),
                 "ref_max_score": float(np.take(ref, cls_idx, axis=channel_axis).max())}
        report[key] = entry
        if entry["box_max_abs"] > tolerance["box_atol"] or entry["score_max_abs"] > tolerance["score_atol"]:
            fatal(f"parity: {runtime} differs from the source model on the {key} input beyond "
                  f"tolerance (box {entry['box_max_abs']:.4g} px vs {tolerance['box_atol']}, "
                  f"score {entry['score_max_abs']:.4g} vs {tolerance['score_atol']})")
    return report


def _sigmoid(x: Any) -> Any:
    import numpy as np

    return 1.0 / (1.0 + np.exp(-np.clip(x, -60.0, 60.0)))


def _rfdetr_pairs(boxes: Any, probs: Any, floor: float) -> List[Tuple[int, int, float, Any]]:
    """(query, class, probability, box) for every pair at or above `floor`."""
    import numpy as np

    queries, classes = np.nonzero(probs >= floor)
    return [(int(q), int(c), float(probs[q, c]), boxes[q]) for q, c in zip(queries, classes)]


def _rfdetr_slot_agreement(rb: Any, rp: Any, gb: Any, gp: Any, box_atol: float,
                           score_atol: float) -> Tuple[float, float, float]:
    """Fraction of query slots with a one-to-one partner (linear assignment)
    that has the same top class, box within box_atol and top probability
    within score_atol; plus the worst box / probability difference over the
    assigned pairs. Slots are described by (box, top class, top probability),
    so memory stays O(Q^2) whatever C is (pure numpy + scipy)."""
    import numpy as np
    from scipy.optimize import linear_sum_assignment

    r_cls, g_cls = rp.argmax(axis=1), gp.argmax(axis=1)
    r_top, g_top = rp.max(axis=1), gp.max(axis=1)
    box_d = np.abs(rb[:, None, :] - gb[None, :, :]).max(axis=2)
    score_d = np.abs(r_top[:, None] - g_top[None, :])
    agree = (box_d <= box_atol) & (score_d <= score_atol) & (r_cls[:, None] == g_cls[None, :])
    # Maximise the number of agreeing pairs first, then prefer closer pairs.
    closeness = np.minimum(box_d / box_atol + score_d / score_atol, 1e3) * 1e-6
    rows, cols = linear_sum_assignment((~agree).astype(np.float64) + closeness)
    matched = agree[rows, cols]
    return (float(matched.sum()) / rb.shape[0], float(box_d[rows, cols].max()),
            float(score_d[rows, cols].max()))


def compare_rfdetr_parity(reference: Dict[str, List[Any]], got: Dict[str, List[Any]],
                          num_classes: int, tolerance: Dict[str, float],
                          runtime: str) -> Dict[str, Any]:
    """Detection-level (permutation-invariant) plus slot-agreement parity; see
    PARITY_TOLERANCE. Pure numpy; FATAL beyond tolerance."""
    import numpy as np

    box_atol, score_atol = tolerance["box_atol"], tolerance["score_atol"]
    floor = tolerance["score_floor"]
    report: Dict[str, Any] = {}
    for key, ref_list in reference.items():
        ref_boxes, ref_logits = (np.asarray(a, dtype=np.float64) for a in ref_list)
        outs = [np.asarray(a, dtype=np.float64) for a in got[key]]
        got_boxes = next((a for a in outs if a.shape == ref_boxes.shape), None)
        got_logits = next((a for a in outs if a.shape == ref_logits.shape and a is not got_boxes),
                          None)
        if got_boxes is None or got_logits is None:
            fatal(f"parity: {runtime} {key} outputs {[list(a.shape) for a in outs]} do not match "
                  f"the source model's {list(ref_boxes.shape)} + {list(ref_logits.shape)}")
        rb, gb = ref_boxes[0], got_boxes[0]
        rp = _sigmoid(ref_logits[0][:, :num_classes])
        gp = _sigmoid(got_logits[0][:, :num_classes])

        # (2) slot agreement, one-to-one: near-tied proposals may swap slots,
        # so pair slots by assignment rather than by index
        agreement, slot_box_max, slot_score_max = _rfdetr_slot_agreement(
            rb, rp, gb, gp, box_atol, score_atol)

        # (1) one-to-one matching of emitted pairs, strongest reference first
        ref_pairs = sorted(_rfdetr_pairs(rb, rp, floor), key=lambda p: -p[2])
        candidates = _rfdetr_pairs(gb, gp, max(0.0, floor - score_atol))
        used: set = set()
        worst_box = worst_score = 0.0
        missing = []
        for q, c, p, box in ref_pairs:
            best = None
            for j, (_q2, c2, p2, box2) in enumerate(candidates):
                if j in used or c2 != c:
                    continue
                d = float(np.abs(box - box2).max())
                if best is None or d < best[0]:
                    best = (d, j, p2)
            if best is None or best[0] > box_atol or abs(p - best[2]) > score_atol:
                missing.append({"query": q, "class": c, "score": round(p, 4),
                                "nearest_box_diff": None if best is None else round(best[0], 5),
                                "nearest_score": None if best is None else round(best[2], 4)})
                continue
            used.add(best[1])
            worst_box, worst_score = max(worst_box, best[0]), max(worst_score, abs(p - best[2]))
        extra = [{"query": q2, "class": c2, "score": round(p2, 4)}
                 for j, (q2, c2, p2, _b) in enumerate(candidates)
                 if j not in used and p2 >= floor + score_atol]
        entry = {"detections": len(ref_pairs), "matched": len(ref_pairs) - len(missing),
                 "box_max_abs": worst_box, "score_max_abs": worst_score,
                 "slot_agreement": round(agreement, 4),
                 "slot_box_max_abs": slot_box_max,
                 "slot_score_max_abs": slot_score_max}
        report[key] = entry
        if missing or extra:
            fatal(f"parity: {runtime} detections on the {key} input differ from the source "
                  f"model's (score >= {floor}): {len(missing)} of {len(ref_pairs)} unmatched "
                  f"within box {box_atol} / score {score_atol} {missing[:3]}; "
                  f"{len(extra)} extra {extra[:3]}")
        if agreement < tolerance["min_slot_agreement"]:
            fatal(f"parity: only {agreement:.1%} of the {rb.shape[0]} query slots agree between "
                  f"{runtime} and the source model on the {key} input (need "
                  f"{tolerance['min_slot_agreement']:.0%} within box {box_atol} / score "
                  f"{score_atol}; worst box {entry['slot_box_max_abs']:.4g}, score "
                  f"{entry['slot_score_max_abs']:.4g})")
    return report


def parity_summary(results: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, float]:
    """Worst box / score difference over every runtime and input (pure)."""
    box = max((e["box_max_abs"] for per_input in results.values() for e in per_input.values()),
              default=0.0)
    score = max((e["score_max_abs"] for per_input in results.values() for e in per_input.values()),
                default=0.0)
    return {"box_max_abs": box, "score_max_abs": score}


# ---------------------------------------------------------------------------
# YOLO (ultralytics)
# ---------------------------------------------------------------------------

def _qualname(obj: Any) -> str:
    cls = obj if isinstance(obj, type) else type(obj)
    return f"{cls.__module__}.{cls.__name__}"


def describe_yolo(yolo: Any) -> Dict[str, Any]:
    net = getattr(yolo, "model", None)
    head = net.model[-1] if net is not None and hasattr(net, "model") else None
    names = getattr(net, "names", None) or {}
    if isinstance(names, dict):
        ordered = [str(names[k]) for k in sorted(names, key=lambda k: int(k))]
    else:
        ordered = [str(n) for n in names]
    return {
        "task": getattr(yolo, "task", None),
        "model_class": _qualname(net) if net is not None else None,
        "head_class": _qualname(head) if head is not None else None,
        "num_classes": int(getattr(head, "nc", len(ordered)) or len(ordered)),
        "class_names": ordered,
        "end2end": bool(getattr(net, "end2end", False)),
        "train_imgsz": (getattr(net, "args", {}) or {}).get("imgsz"),
    }


def gate_yolo(desc: Dict[str, Any], cfg: SimpleNamespace) -> None:
    """The authoritative detector check (Req 6.1, 6.2)."""
    if desc["task"] != "detect":
        fatal(f"checkpoint task is {desc['task']!r}; only object detection ('detect') converts")
    if desc["model_class"] != YOLO_MODEL_CLASS:
        fatal(f"checkpoint model class is {desc['model_class']}; only {YOLO_MODEL_CLASS} converts")
    if desc["head_class"] not in YOLO_ACCEPTED_HEADS:
        fatal(f"checkpoint detection head is {desc['head_class']}; accepted: "
              f"{', '.join(YOLO_ACCEPTED_HEADS)}")
    if desc["num_classes"] != cfg.num_classes:
        fatal(f"checkpoint has {desc['num_classes']} classes; the import recorded {cfg.num_classes}")


# Checkpoint overrides that could change the graph's precision, layout or
# post-processing; never forwarded from the (untrusted) checkpoint.
_YOLO_OVERRIDES_DROPPED = ("half", "int8", "quantize", "dynamic", "nms", "simplify", "optimize",
                           "format", "keras", "end2end", "agnostic_nms", "fraction", "workspace")


def yolo_export_args(overrides: Dict[str, Any], cfg: SimpleNamespace) -> Dict[str, Any]:
    """Model.export's argument assembly ({**overrides, imgsz, data: None, ...,
    mode: 'export'}) with our pinned arguments winning and any precision /
    post-processing override from the checkpoint dropped (pure)."""
    kept = {k: v for k, v in (overrides or {}).items() if k not in _YOLO_OVERRIDES_DROPPED}
    return {**kept, "imgsz": cfg.network_input, "data": None, **YOLO_EXPORT_ARGS,
            "opset": cfg.opset, "mode": "export"}


def convert_yolo(checkpoint: Path, cfg: SimpleNamespace) -> SimpleNamespace:
    applied = disable_autoupdate()
    log(f"ultralytics AutoUpdate disabled: {applied}")
    import torch
    import ultralytics
    from ultralytics import YOLO
    from ultralytics.engine.exporter import Exporter

    try:
        yolo = YOLO(str(checkpoint))
    except Exception as e:  # noqa: BLE001
        fatal(f"ultralytics {ultralytics.__version__} could not load the checkpoint: "
              f"{type(e).__name__}: {e}")
    desc = describe_yolo(yolo)
    log(f"checkpoint: {json.dumps(desc)}")
    gate_yolo(desc, cfg)

    # Model.export's own argument assembly, but holding on to the Exporter so
    # the parity reference is the exact module that was traced.
    exporter = Exporter(overrides=yolo_export_args(yolo.overrides, cfg), _callbacks=yolo.callbacks)
    try:
        produced = exporter(model=yolo.model)
    except Exception as e:  # noqa: BLE001
        fatal(f"ultralytics ONNX export failed: {type(e).__name__}: {e}")
    produced = Path(str(produced))
    if not produced.is_file():
        fatal(f"ultralytics export produced no file at {produced}")
    reference_module = exporter.model

    def reference(batch: Any) -> List[Any]:
        with torch.no_grad():
            out = reference_module(torch.from_numpy(batch))
        if isinstance(out, (list, tuple)):
            out = out[0]
        return [out.detach().cpu().numpy()]

    return SimpleNamespace(onnx_path=produced, reference=reference, describe=desc,
                           exporter=f"ultralytics {ultralytics.__version__}",
                           torch=torch.__version__)


# ---------------------------------------------------------------------------
# RF-DETR
# ---------------------------------------------------------------------------

def _rfdetr_state_dict(ckpt: Any) -> Dict[str, Any]:
    """The detector weights of an rfdetr checkpoint in any of the three layouts
    seen in the wild (spike §1.4 of docs/transfer-learning-spike.md): published
    {model, args(Namespace)}, 1.10.1 callback {model, args(dict), ...} and the
    PTL payload {state_dict: {"model.*": ...}}."""
    if not isinstance(ckpt, dict):
        fatal(f"RF-DETR checkpoint is a {type(ckpt).__name__}, not a dict")
    model = ckpt.get("model")
    if isinstance(model, dict) and model:
        return model
    state = ckpt.get("state_dict")
    if isinstance(state, dict):
        stripped = {k[len("model."):]: v for k, v in state.items() if k.startswith("model.")}
        if stripped:
            return stripped
    fatal("RF-DETR checkpoint holds no model state_dict")
    raise AssertionError  # unreachable


def _shape_signature(state: Dict[str, Any]) -> Dict[str, Tuple[int, ...]]:
    return {k: tuple(v.shape) for k, v in state.items()
            if hasattr(v, "shape") and not k.endswith(RFDETR_IGNORED_KEYS)}


def rfdetr_architecture_mismatch(expected: Dict[str, Tuple[int, ...]],
                                 checkpoint: Dict[str, Tuple[int, ...]]) -> Dict[str, Any]:
    """Key and shape differences between an architecture and a checkpoint
    (pure). Empty lists everywhere == the checkpoint IS that architecture."""
    missing = sorted(set(expected) - set(checkpoint))
    unexpected = sorted(set(checkpoint) - set(expected))
    shapes = sorted(k for k in set(expected) & set(checkpoint) if expected[k] != checkpoint[k])
    return {"missing": missing, "unexpected": unexpected, "shape_mismatch": shapes}


def convert_rfdetr(checkpoint: Path, cfg: SimpleNamespace) -> SimpleNamespace:
    import copy

    import rfdetr
    import torch

    import train_rfdetr as trainer  # the trainer's pure export/verify helpers

    try:
        # Isolated job: this is the one place a full unpickle is permitted.
        ckpt = torch.load(str(checkpoint), map_location="cpu", weights_only=False)  # nosem: runs only inside the network-isolated, credential-less Conversion_Job
    except Exception as e:  # noqa: BLE001
        fatal(f"could not load the RF-DETR checkpoint: {type(e).__name__}: {e}")
    state = _rfdetr_state_dict(ckpt)
    num_classes = trainer.checkpoint_num_classes(ckpt)
    if num_classes != cfg.num_classes:
        fatal(f"checkpoint head has {num_classes} classes; the import recorded {cfg.num_classes}")
    if any(k.startswith(("segmentation_head.", "mask_")) for k in state):
        fatal("checkpoint carries a segmentation head; only RF-DETR detection converts")
    ckpt_sig = _shape_signature(state)

    candidates = [cfg.rfdetr_size] if cfg.rfdetr_size else list(RFDETR_SIZE_CLASSES)
    model_name = ckpt.get("model_name") if isinstance(ckpt, dict) else None
    if model_name in RFDETR_PML_MODEL_NAMES:
        fatal(f"checkpoint is {model_name}, a PML-licensed RF-DETR size (rfdetr_plus); only the "
              f"Apache-2.0 sizes {', '.join(RFDETR_SIZE_CLASSES.values())} convert")
    if isinstance(model_name, str) and model_name.startswith(RFDETR_NON_DETECTION_PREFIXES):
        fatal(f"checkpoint is {model_name}; only RF-DETR object detection converts")
    if model_name in RFDETR_LEGACY_MODEL_NAMES:
        fatal(f"checkpoint is the legacy {model_name}; only {', '.join(RFDETR_SIZE_CLASSES.values())} "
              f"convert (rfdetr 1.10.1 sizes)")
    named = next((s for s, c in RFDETR_SIZE_CLASSES.items() if c == model_name), None)
    if named and not cfg.rfdetr_size:
        candidates = [named] + [c for c in candidates if c != named]
    matches, tried = [], {}
    for size in candidates:
        Model = getattr(rfdetr, RFDETR_SIZE_CLASSES[size])
        try:
            shell = Model(pretrain_weights=None, num_classes=num_classes)
        except Exception as e:  # noqa: BLE001
            tried[size] = f"could not build: {e}"
            continue
        diff = rfdetr_architecture_mismatch(_shape_signature(shell.model.model.state_dict()), ckpt_sig)
        tried[size] = {k: len(v) for k, v in diff.items()}
        if not any(diff.values()):
            matches.append(size)
            if named == size:
                break
    log(f"RF-DETR size candidates: {json.dumps(tried)}")
    if len(matches) != 1:
        fatal(f"checkpoint matches {len(matches)} Apache-2.0 RF-DETR sizes ({matches or 'none'}); "
              f"per-size key/shape differences: {tried}")
    size = matches[0]
    Model = getattr(rfdetr, RFDETR_SIZE_CLASSES[size])
    try:
        model = Model(pretrain_weights=str(checkpoint), trust_checkpoint=True, num_classes=num_classes)
    except Exception as e:  # noqa: BLE001
        fatal(f"could not load weights into {RFDETR_SIZE_CLASSES[size]}: {type(e).__name__}: {e}")
    native = int(model.model.resolution)
    if cfg.network_input != native:
        fatal(f"RF-DETR exports at the checkpoint's own resolution ({native} for {size}); "
              f"the import requested {cfg.network_input}")
    loaded = rfdetr_architecture_mismatch(_shape_signature(model.model.model.state_dict()), ckpt_sig)
    if any(loaded.values()):
        fatal(f"weights did not load key-for-key into {RFDETR_SIZE_CLASSES[size]}: {loaded}")

    export_cfg = SimpleNamespace(opset=cfg.opset)
    produced = trainer.export_onnx(model, export_cfg, WORK / "rfdetr_export")

    reference_module = copy.deepcopy(model.model.model).cpu().eval()
    reference_module.export()

    def reference(batch: Any) -> List[Any]:
        with torch.no_grad():
            out = reference_module(torch.from_numpy(batch))
        if isinstance(out, dict):
            out = (out["pred_boxes"], out["pred_logits"])
        return [t.detach().cpu().numpy() for t in out[:2]]

    desc = {"size": size, "model_class": RFDETR_SIZE_CLASSES[size], "num_classes": num_classes,
            "class_names": trainer.checkpoint_class_names(ckpt), "resolution": native}
    return SimpleNamespace(onnx_path=Path(produced), reference=reference, describe=desc,
                           exporter=f"rfdetr {distribution_version('rfdetr')}",
                           torch=torch.__version__, trainer=trainer)


def verify_rfdetr_contract(summary: Dict[str, Any], network_input: int, num_classes: int,
                           trainer: Any) -> Dict[str, Any]:
    """The trainer's verify_input / verify_two_outputs on the same shapes, plus
    the float32 requirement (pure apart from the trainer import)."""
    input_shape = verify_input_contract(summary, network_input)
    for name, _shape, dtype in summary["outputs"]:
        if dtype != "FLOAT":
            fatal(f"ONNX output {name} is {dtype}; expected float32 (FLOAT)")
    try:
        queries, boxes, logits = trainer.verify_two_outputs(
            [(n, list(s)) for n, s, _t in summary["outputs"]], num_classes)
    except ValueError as e:
        fatal(str(e).removeprefix("FATAL: "))
    return {"input_shape": input_shape, "output_shapes": [boxes, logits], "top_k": queries}


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------

def build_conversion_metadata(cfg: SimpleNamespace, result: SimpleNamespace,
                              summary: Dict[str, Any], contract: Dict[str, Any],
                              floor: SimpleNamespace, parity: Dict[str, Any],
                              source_sha256: str, onnx_sha256: str,
                              ir: Tuple[int, int]) -> Dict[str, Any]:
    """training_metadata.json: the keys package_trained_detection_component
    reads, with the trainers' meaning, plus provenance (Req 6.8; pure)."""
    desc = result.describe
    class_names = [str(c) for c in (desc.get("class_names") or [])]
    common = {
        "detection_arch": cfg.arch,
        "num_classes": cfg.num_classes,
        "class_names": class_names,
        "opset": summary["opsets"].get("ai.onnx"),
        "onnx_opset": summary["opsets"].get("ai.onnx"),
        "ir_version": summary["ir_version"],
        "ir_version_exported": ir[0],
        "exporter": result.exporter,
        "torch": result.torch,
        "source_sha256": source_sha256,
        "onnx_sha256": onnx_sha256,
        "fleet_floor": {"onnxruntime": floor.ort, "loaded": True, "finite": True},
        "parity": {"tolerance": PARITY_TOLERANCE[cfg.arch], "max_abs": parity_summary(parity),
                   "inputs": sorted({k for per_input in parity.values() for k in per_input}),
                   "runtimes": parity},
        "checkpoint": desc,
        "conversion": True,
    }
    if cfg.arch == "yolo":
        return {
            **common,
            "imgsz": cfg.network_input,
            "onnx_input_shape": contract["input_shape"],
            "onnx_output_shape": contract["output_shape"],
            "device_manifest_hints": {
                "preserve_aspect": True,
                "network_input": cfg.network_input,
                "layout": "yolo",
                "iou_threshold": 0.45,
                "score_threshold": 0.25,
            },
        }
    boxes, logits = contract["output_shapes"]
    return {
        **common,
        "rfdetr_size": desc["size"],
        "model_class": desc["model_class"],
        "resolution": cfg.network_input,
        "onnx_input_shape": contract["input_shape"],
        "onnx_output_shapes": [boxes, logits],
        "logits_slots": int(logits[2]),
        "background_slot": int(logits[2]) - 1,
        "top_k": int(contract["top_k"]),
        "device_manifest_hints": {
            "layout": "rf_detr",
            "stage_type": "rf_detr_object_detection",
            "network_input": cfg.network_input,
            "preserve_aspect": False,
            "normalize": True,
            "score_threshold": 0.5,
            "top_k": int(contract["top_k"]),
        },
    }


def write_artifact(onnx_path: Path, metadata: Dict[str, Any], model_dir: Optional[Path] = None) -> None:
    """/opt/ml/model holds exactly model.onnx + training_metadata.json."""
    model_dir = MODEL_DIR if model_dir is None else model_dir
    model_dir.mkdir(parents=True, exist_ok=True)
    for entry in model_dir.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    shutil.copyfile(onnx_path, model_dir / "model.onnx")
    (model_dir / "training_metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(env: Dict[str, str]) -> Dict[str, Any]:
    started = time.time()
    cfg = read_config(env)
    log(f"config: {json.dumps(vars(cfg))}")
    WORK.mkdir(parents=True, exist_ok=True)
    source = single_input_file(INPUT_DIR)
    digest = sha256_file(source)
    if digest != cfg.expected_sha256:
        fatal(f"checkpoint sha256 {digest} differs from the one the portal recorded "
              f"({cfg.expected_sha256}); the input changed after import")
    log(f"checkpoint {source.name}: {source.stat().st_size} bytes, sha256 {digest}")
    local = WORK / f"checkpoint{source.suffix.lower() or '.pt'}"
    shutil.copyfile(source, local)

    result = convert_yolo(local, cfg) if cfg.arch == "yolo" else convert_rfdetr(local, cfg)
    ir = normalize_ir_version(result.onnx_path)
    if ir[0] != ir[1]:
        log(f"IR version clamped {ir[0]} -> {ir[1]} for onnxruntime {FLEET_FLOOR_ORT}")
    summary = onnx_summary(result.onnx_path)
    log(f"ONNX: {json.dumps({k: v for k, v in summary.items() if k != 'metadata_props'})}")
    verify_fleet_structure(summary)
    if cfg.arch == "yolo":
        contract = verify_yolo_contract(summary, cfg.network_input, cfg.num_classes)
    else:
        contract = verify_rfdetr_contract(summary, cfg.network_input, cfg.num_classes, result.trainer)
    log(f"contract OK: {json.dumps(contract)}")

    inputs = parity_inputs(cfg.arch, cfg.network_input)
    reference = {key: result.reference(batch) for key, batch in inputs.items()}
    current = run_on_export_runtime(result.onnx_path, inputs)
    floor = run_on_fleet_floor(result.onnx_path, inputs)
    compare = compare_yolo_parity if cfg.arch == "yolo" else compare_rfdetr_parity
    parity: Dict[str, Any] = {}
    for runtime in (current, floor):
        label = f"onnxruntime {runtime.ort}"
        parity[label] = compare(reference, runtime.outputs, cfg.num_classes,
                                PARITY_TOLERANCE[cfg.arch], label)
        log(f"parity vs {label} OK: {json.dumps(parity[label])}")

    onnx_sha = sha256_file(result.onnx_path)
    metadata = build_conversion_metadata(cfg, result, summary, contract, floor, parity,
                                         digest, onnx_sha, ir)
    metadata["export_seconds"] = round(time.time() - started, 1)
    write_artifact(result.onnx_path, metadata)
    log(f"wrote {MODEL_DIR / 'model.onnx'} ({(MODEL_DIR / 'model.onnx').stat().st_size} bytes, "
        f"sha256 {onnx_sha}) + training_metadata.json in {metadata['export_seconds']} s")
    return metadata


def main(argv: Optional[Sequence[str]] = None) -> int:
    # SageMaker runs the image as `<entrypoint> train`; there is nothing to parse.
    try:
        run(dict(os.environ))
        log("DONE")
        return 0
    except SystemExit as e:
        reason = str(e.code) if e.code not in (None, 0) else "FATAL: export aborted"
        log(reason)
        write_failure(reason)
        return 1
    except Exception as e:  # noqa: BLE001 - every failure must reach FailureReason
        traceback.print_exc()
        reason = f"FATAL: {type(e).__name__}: {e}"
        write_failure(reason)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
