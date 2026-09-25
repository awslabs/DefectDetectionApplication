#!/usr/bin/env python3
"""SageMaker script-mode entry point: fine-tune an RF-DETR detector on a DDA
ObjectDetection manifest and export ONNX for the DDA edge runtime.

Sibling of train.py (YOLO); same artifact contract, different geometry:

  1. download the labeling job's output manifest (+ images) from S3
  2. build an RF-DETR ("roboflow") COCO dataset via the bundled
     manifest_to_detector_dataset.py --format coco --coco-layout rfdetr
     (train/valid/test/_annotations.coco.json, images beside the JSON)
  3. fine-tune RFDETR{Nano,Small,Medium,Large} from the published COCO weights
     or from BASE_WEIGHTS_S3 (a prior job's checkpoint_best_total.pth)
  4. evaluate the best checkpoint on the leakage-safe `test` split and print
     one `TEST METRICS: {json}` line (same keys as train.py)
  5. export ONNX from checkpoint_best_total.pth at RESOLUTION, static batch 1,
     and verify the graph has exactly two outputs [1, Q, 4] + [1, Q, C+1]
  6. write model.onnx + checkpoint_best_total.pth + training_metadata.json
     to /opt/ml/model

Geometry contract: RF-DETR trains on a SQUARE RESIZE (aspect-destroying, no
letterbox) with ImageNet mean/std normalisation. The device manifest must say
preserve_aspect=false, normalize=true -- the opposite of YOLO.

Class layout (rfdetr 1.10.1, datasets/coco.py + docs/learn/export.md): the
"roboflow" loader remaps COCO category ids to contiguous 0-based labels in
category-id order, so label i == class_names[i]. The classification head
allocates num_classes + 1 logit slots; the extra LAST slot is the never-positive
background slot. The exported `labels` tensor is therefore [1, Q, C + 1] and
the device's RfDetrDetectionPostProcessor (class_names[slot]) is correct for
slots 0..C-1. verify_two_outputs accepts ONLY C + 1: a C-slot graph is the
signature of a head that was never widened to the manifest (spike job
tl13-rfdetr-nc2-0603 exported labels [1,300,2] for a 2-class manifest) and
is FATAL (Req 1.6, 7.4).

rfdetr API pins (verified against the 1.10.1 source, then confirmed end to
end on SageMaker by the task-1.3 baseline job `tl13-rfdetr-base-0506`,
2026-09-15 -- see docs/transfer-learning-spike.md section 3: .train() accepted
every kwarg below, .evaluate() returned the "test/*" keys, .export() wrote
model.onnx with outputs dets [1,300,4] + labels [1,300,C+1]):
  * RFDETR*(pretrain_weights=<path>, trust_checkpoint=True, num_classes=C)
    -- num_classes MUST be pinned on this path: without it the 1.10.1 loader
    adopts the checkpoint's class count as if the caller had set it, and
    .train() then keeps the old head even when the dataset has more classes
    (spike job tl13-rfdetr-nc2-0603 shipped a 1-class head for a 2-class
    manifest; see build_model). The pin was validated end to end by
    tl13-rfdetr-nc2b-0625 (1-class base onto a 2-class manifest -> labels
    [1,300,3]). Published-weights path: no kwargs.
  * .train(dataset_dir, epochs, batch_size, grad_accum_steps, lr, output_dir,
           resolution, early_stopping, early_stopping_patience, run_test,
           tensorboard, wandb, class_names)   -- TrainConfig has extra="forbid"
  * .evaluate(split="test", dataset_dir=...) -> {"test/mAP_50_95", "test/mAP_50",
           "test/precision", "test/recall", "test/F1", "test/mAR"}
  * .export(output_dir, opset_version, batch_size=1, dynamic_batch=False,
            format="onnx", output_name=...) -> Path; outputs named dets, labels
  * best checkpoint: <output_dir>/checkpoint_best_total.pth

The S3 / converter / metadata plumbing lives in the sibling _common.py
(shared with train.py); SageMaker extracts sourcedir.tar.gz flat, so it is
imported as a plain sibling module. rfdetr / torch / onnx are imported lazily
inside functions so this module imports on a bare host (unit tests).
"""
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from _common import (
    MODEL_DIR,
    WORK,
    cap_onnx_ir_version,
    fetch_base_weights,
    hp,
    run_converter,
    stage_manifest_and_images,
    write_metadata,
)

# RF_DETR_Size -> (rfdetr class name, native square resolution). xlarge/2xlarge
# are PML-licensed and deliberately excluded.
SIZE_CLASSES = {
    "nano": ("RFDETRNano", 384),
    "small": ("RFDETRSmall", 512),
    "medium": ("RFDETRMedium", 576),
    "large": ("RFDETRLarge", 704),
}
DEFAULT_SIZE = "small"

# DINOv2 patch/window grid: patch_size * num_windows == 32 for the current
# Nano/Small/Medium/Large checkpoints (rfdetr FAQ "What input resolutions are
# allowed?"; rfdetr raises ValueError for anything else). The [224, 1120]
# range mirrors the portal's boundary validation (detection_training.py).
RESOLUTION_STEP = 32
RESOLUTION_MIN = 224
RESOLUTION_MAX = 1120
RESOLUTION_FATAL = ("FATAL: RESOLUTION must be a multiple of 32 between "
                    f"{RESOLUTION_MIN} and {RESOLUTION_MAX}")

# The fine-tunable checkpoint this entry point leaves in its artifact, and the
# member to pull from a prior job's model.tar.gz when BASE_WEIGHTS_MEMBER is
# not given explicitly.
CHECKPOINT_MEMBER = "checkpoint_best_total.pth"
# Decoder query slots for Nano..Large (num_queries == num_select == 300); the
# device keeps the top_k query/class pairs above score_threshold (no NMS).
TOP_K = 300
# Device manifest defaults for the rf_detr_object_detection stage.
DEVICE_SCORE_THRESHOLD = 0.5
STAGE_TYPE = "rf_detr_object_detection"
# rfdetr's ONNX export names (export/main.py): input "input", outputs
# ("dets", "labels"). Used to disambiguate when num_classes + 1 == 4.
ONNX_INPUT_NAME = "input"
ONNX_BOXES_NAME = "dets"
ONNX_LOGITS_NAME = "labels"

# Metric keys the portal's DETECTION_METRIC_DEFINITIONS regexes capture from the
# single `TEST METRICS: {...}` log line (identical to train.py's).
METRIC_KEYS = ("test_map50", "test_map50_95", "test_precision", "test_recall")
# rfdetr .evaluate() key -> our key (the split prefix is added per call).
_EVAL_KEY_MAP = {
    "mAP_50": "test_map50",
    "mAP_50_95": "test_map50_95",
    "precision": "test_precision",
    "recall": "test_recall",
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def native_resolution(size: str) -> int:
    """The published checkpoint's square resolution for an RF_DETR_Size."""
    return SIZE_CLASSES[size][1]


def validate_resolution(resolution) -> int:
    """Return `resolution` as an int, or exit FATAL when it is not a multiple of
    32 in [224, 1120] (Req 1.5; checked before anything is downloaded)."""
    try:
        r = int(resolution)
    except (TypeError, ValueError):
        sys.exit(f"{RESOLUTION_FATAL} (got {resolution!r})")
    if r <= 0 or r % RESOLUTION_STEP or r < RESOLUTION_MIN or r > RESOLUTION_MAX:
        sys.exit(f"{RESOLUTION_FATAL} (got {r})")
    return r


def read_config() -> SimpleNamespace:
    """Read the portal's hyperparameters from the environment.

    RESOLUTION defaults to the size's native resolution and is validated here,
    so a bad value fails before any S3 download.
    """
    size = (hp("RFDETR_SIZE", DEFAULT_SIZE) or DEFAULT_SIZE).strip().lower()
    if size not in SIZE_CLASSES:
        sys.exit(f"FATAL: RFDETR_SIZE must be one of {', '.join(SIZE_CLASSES)}; got {size!r}")
    resolution = hp("RESOLUTION")
    if resolution is None or str(resolution).strip() == "":
        resolution = native_resolution(size)
    return SimpleNamespace(
        manifest_s3=hp("MANIFEST_S3"),
        images_s3=hp("IMAGES_S3"),
        size=size,
        model_class=SIZE_CLASSES[size][0],
        resolution=validate_resolution(resolution),
        epochs=hp("EPOCHS", "100", int),
        batch=hp("BATCH", "4", int),
        grad_accum=hp("GRAD_ACCUM", "4", int),
        lr=hp("LR", "1e-4", float),
        patience=hp("PATIENCE", "10", int),
        opset=hp("ONNX_OPSET", "17", int),
        base_weights_s3=hp("BASE_WEIGHTS_S3"),
        base_weights_member=hp("BASE_WEIGHTS_MEMBER"),
    )


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def _annotations_path(dataset_dir: Path, split: str) -> Path:
    return Path(dataset_dir) / split / "_annotations.coco.json"


def assert_splits(dataset_dir: Path, splits: Sequence[str] = ("train", "valid")) -> None:
    """Exit FATAL naming the split if any required split is missing or empty
    (Req 1.3). rfdetr would otherwise fail minutes later with a stack trace."""
    for split in splits:
        ann = _annotations_path(dataset_dir, split)
        if not ann.is_file():
            sys.exit(f"FATAL: dataset split '{split}' is missing: no {ann}")
        try:
            data = json.loads(ann.read_text())
        except ValueError as e:
            sys.exit(f"FATAL: dataset split '{split}' has an unreadable {ann.name}: {e}")
        if not data.get("images"):
            sys.exit(f"FATAL: dataset split '{split}' is empty: {ann} lists no images")


def read_class_names(dataset_dir: Path) -> List[str]:
    """Class names from the train split's `categories`, sorted by id.

    The converter writes 1-based ids in class_names order and no parent /
    super-category entry, and rfdetr's roboflow loader assigns labels by
    position in the id-sorted category list -- so index i here is logit slot i.
    """
    data = json.loads(_annotations_path(dataset_dir, "train").read_text())
    cats = sorted(data.get("categories", []), key=lambda c: int(c["id"]))
    return [str(c["name"]) for c in cats]


def stage_dataset(cfg) -> Tuple[Path, List[str]]:
    """Download manifest + images, build the RF-DETR COCO layout, check splits."""
    manifest, images = stage_manifest_and_images(cfg.manifest_s3, cfg.images_s3, WORK)
    dataset = run_converter(manifest, images, WORK / "dataset", "coco",
                            coco_layout="rfdetr")
    assert_splits(dataset, ("train", "valid"))
    if not _annotations_path(dataset, "test").is_file():
        print("WARN: no test split; TEST METRICS will come from the valid split",
              flush=True)
    class_names = read_class_names(dataset)
    if not class_names:
        sys.exit("FATAL: train split has no categories")
    print(f"dataset: {dataset}\nclasses ({len(class_names)}): {class_names}", flush=True)
    return dataset, class_names


# ---------------------------------------------------------------------------
# Base weights (transfer learning)
# ---------------------------------------------------------------------------

def _get(obj, key, default=None):
    """dict key or attribute (rfdetr checkpoints store `args` as a dict in the
    PTL era and as an argparse.Namespace in legacy ones)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def checkpoint_class_names(ckpt) -> Optional[List[str]]:
    """Best-effort class list from a loaded rfdetr checkpoint dict (pure)."""
    if not isinstance(ckpt, dict):
        return None
    for holder in (ckpt, _get(ckpt, "args"), _get(ckpt, "model_config"),
                   _get(ckpt, "train_config")):
        if holder is None:
            continue
        names = _get(holder, "class_names")
        if isinstance(names, (list, tuple)) and names:
            return [str(n) for n in names]
    return None


def checkpoint_num_classes(ckpt) -> Optional[int]:
    """num_classes from the classification head shape (slots - 1, the extra
    slot being background), else from the saved config; None if unknown."""
    if not isinstance(ckpt, dict):
        return None
    state = ckpt.get("model") or ckpt.get("state_dict") or {}
    if isinstance(state, dict):
        for key, value in state.items():
            if str(key).endswith("class_embed.weight") and hasattr(value, "shape"):
                try:
                    return int(value.shape[0]) - 1
                except (TypeError, IndexError, ValueError):
                    break
    for holder in (_get(ckpt, "model_config"), _get(ckpt, "args")):
        n = _get(holder, "num_classes") if holder is not None else None
        if isinstance(n, int):
            return n
    return None


def log_checkpoint_classes(path: Path, manifest_classes: List[str]) -> None:
    """Log the base checkpoint's classes against the manifest's (Req 7.4).

    rfdetr adapts the detection head itself when the dataset's class count
    differs from the checkpoint's (train() -> reinitialize_detection_head);
    this only makes old/new visible in the job log. Never fatal.
    """
    try:
        import torch

        ckpt = torch.load(str(path), map_location="cpu", weights_only=False)
        old = checkpoint_class_names(ckpt)
        n_old = checkpoint_num_classes(ckpt)
        if old is None and n_old is None:
            print(f"base checkpoint {path.name}: no class information found", flush=True)
            return
        shown = old if old is not None else f"{n_old} classes (names not stored)"
        if (old is not None and old != list(manifest_classes)) or \
                (old is None and n_old != len(manifest_classes)):
            print(f"base checkpoint classes: {shown}\n"
                  f"manifest classes ({len(manifest_classes)}): {list(manifest_classes)}\n"
                  f"class set differs: rfdetr will re-initialise the detection head",
                  flush=True)
        else:
            print(f"base checkpoint classes match the manifest: {list(manifest_classes)}",
                  flush=True)
    except Exception as e:  # noqa: BLE001 - diagnostics only
        print(f"WARN: could not compare class names: {e}", flush=True)


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def model_class(cfg):
    import rfdetr

    return getattr(rfdetr, cfg.model_class)


def build_model(Model, cfg, base: Optional[Path], num_classes: Optional[int] = None):
    """Instantiate the size class from the published COCO weights, or from the
    resolved base checkpoint when BASE_WEIGHTS_S3 was set (Req 1.4, 6.5).

    `num_classes` (the manifest's class count) is pinned explicitly on the
    base-weights path. Without it, rfdetr 1.10.1's load_pretrain_weights
    auto-aligns model_config.num_classes to the checkpoint's head and that
    assignment marks the field as user-set, so .train()'s
    _align_num_classes_from_dataset then refuses to widen the head to the
    dataset ("Dataset ... has 2 classes but model was initialized with
    num_classes=1. Using the model's configured value (1)") -- spike run
    tl13-rfdetr-nc2-0603 exported a 1-class head for a 2-class manifest.
    Pinning makes the constructor keep the configured count and re-initialise
    the head to it after loading (weights.py: `if checkpoint_num_classes <
    configured ... and user_overrode: reinitialize_detection_head(...)`),
    which is exactly what rfdetr's own warning tells the caller to do. The
    published-weights path is left as it was (its default num_classes is not
    user-set, so .train() aligns it from the dataset -- verified in (b1)).
    """
    kwargs: Dict[str, Any] = {}
    if base is not None:
        kwargs["pretrain_weights"] = str(base)
        # Our own / the user's chosen checkpoint: allow the full-pickle fallback
        # if rfdetr's safe (weights_only) load rejects it. No silent fallback
        # to COCO weights -- training from the wrong base is worse than failing.
        kwargs["trust_checkpoint"] = True
        if num_classes is not None:
            kwargs["num_classes"] = int(num_classes)
    try:
        model = Model(**kwargs)
    except Exception as e:  # noqa: BLE001 - surface as the job's failure reason
        what = f"base weights {base} (from {cfg.base_weights_s3})" if base else \
            f"published {cfg.model_class} weights"
        sys.exit(f"FATAL: could not load {what}: {e}")
    print(f"model: {cfg.model_class} base weights: "
          f"{cfg.base_weights_s3 if base else 'published COCO checkpoint'}", flush=True)
    return model


def train_kwargs(cfg, dataset_dir: Path, class_names: List[str], out_dir: Path) -> Dict[str, Any]:
    """The .train() kwargs (pure; pinned against rfdetr 1.10.1 TrainConfig,
    which has extra="forbid", so every key here must be a real field)."""
    return {
        "dataset_dir": str(dataset_dir),
        "epochs": cfg.epochs,
        "batch_size": cfg.batch,
        "grad_accum_steps": cfg.grad_accum,
        "lr": cfg.lr,
        "output_dir": str(out_dir),
        # Handled by rfdetr's _prepare_run_config: sets model_config.resolution
        # and keeps positional_encoding_size in sync (the constructor does not).
        "resolution": cfg.resolution,
        "early_stopping": True,
        "early_stopping_patience": cfg.patience,
        # Evaluates checkpoint_best_total.pth on the test split at the end of
        # training (logged as test/*); read_test_metrics re-runs it explicitly
        # on the reloaded checkpoint so the TEST METRICS line is deterministic.
        "run_test": True,
        # No logger extras in the container (rfdetr[loggers] not installed).
        "tensorboard": False,
        "wandb": False,
        # Stored in the checkpoint so a later run can compare class sets.
        "class_names": list(class_names),
    }


def train_model(model, cfg, dataset_dir: Path, class_names: List[str]) -> Path:
    """Run .train(); returns the best checkpoint path (FATAL if absent)."""
    out_dir = WORK / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    kwargs = train_kwargs(cfg, dataset_dir, class_names, out_dir)
    print("train kwargs: " + json.dumps(kwargs), flush=True)
    model.train(**kwargs)
    best = out_dir / CHECKPOINT_MEMBER
    if not best.is_file():
        listing = ", ".join(sorted(p.name for p in out_dir.iterdir())) or "(empty)"
        sys.exit(f"FATAL: training produced no {CHECKPOINT_MEMBER} in {out_dir}; "
                 f"contents: {listing}")
    print(f"best checkpoint: {best} ({best.stat().st_size} bytes)", flush=True)
    return best


def reload_best(Model, best: Path, cfg):
    """Reload checkpoint_best_total.pth as a fresh model for eval + export.

    from_checkpoint restores the checkpoint's own model_config (resolution,
    positional_encoding_size, num_classes); the plain constructor path is the
    fallback. The exported input shape is verified against RESOLUTION either
    way.
    """
    from_ckpt = getattr(Model, "from_checkpoint", None)
    if callable(from_ckpt):
        try:
            return from_ckpt(str(best), trust_checkpoint=True)
        except Exception as e:  # noqa: BLE001
            print(f"WARN: {cfg.model_class}.from_checkpoint failed ({e}); "
                  f"falling back to pretrain_weights", flush=True)
    return Model(pretrain_weights=str(best), resolution=cfg.resolution,
                 trust_checkpoint=True)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def metrics_from_eval(result: Dict[str, Any], split: str = "test") -> Dict[str, float]:
    """Map rfdetr's .evaluate() dict ({"<split>/mAP_50": ...}) onto train.py's
    TEST METRICS keys (pure). Missing keys become NaN so the line always has
    the same shape and the portal regexes still match what is there."""
    out: Dict[str, float] = {}
    for src, dst in _EVAL_KEY_MAP.items():
        value = result.get(f"{split}/{src}")
        if value is None:
            value = result.get(src)
        try:
            out[dst] = float(value.item() if hasattr(value, "item") else value)
        except (TypeError, ValueError):
            out[dst] = float("nan")
    # Keep the same key order as train.py's line.
    return {k: out[k] for k in METRIC_KEYS}


def read_test_metrics(model, dataset_dir: Path, cfg) -> Dict[str, float]:
    """Evaluate the reloaded best checkpoint on the converter's leakage-safe
    test split and print the `TEST METRICS: {json}` line (Req 1.8). Falls back
    to the valid split (with a log line) when there is no test split; never
    fatal -- a missing metric must not lose the model."""
    metrics: Dict[str, float] = {}
    split = "test" if _annotations_path(dataset_dir, "test").is_file() else "val"
    if split != "test":
        print("WARN: no test split; evaluating the valid split instead", flush=True)
    try:
        result = model.evaluate(
            split=split,
            dataset_dir=str(dataset_dir),
            batch_size=cfg.batch,
            output_dir=str(WORK / "eval"),
            tensorboard=False,
        )
        raw = {k: (v.item() if hasattr(v, "item") else v) for k, v in dict(result).items()}
        print(f"rfdetr evaluate({split}): {json.dumps(raw, default=str)}", flush=True)
        metrics = metrics_from_eval(dict(result), split)
        print("TEST METRICS: " + json.dumps(metrics), flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"WARN: {split}-split evaluation failed: {e}", flush=True)
    return metrics


# ---------------------------------------------------------------------------
# ONNX export + verification
# ---------------------------------------------------------------------------

def export_onnx(model, cfg, export_dir: Path) -> Path:
    """Export ONNX at the model's (RESOLUTION) input, static batch 1, opset
    ONNX_OPSET; returns the produced file (Req 1.6)."""
    export_dir.mkdir(parents=True, exist_ok=True)
    try:
        produced = model.export(
            output_dir=str(export_dir),
            opset_version=cfg.opset,
            batch_size=1,
            dynamic_batch=False,   # static [1, 3, R, R]; the device runs batch 1
            format="onnx",
            verbose=False,
            output_name="model",   # -> <export_dir>/model.onnx (1.10.1 export/_naming.py)
        )
    except Exception as e:  # noqa: BLE001
        sys.exit(f"FATAL: ONNX export failed: {e}")
    src = Path(str(produced)) if produced else export_dir / "model.onnx"
    if not src.is_file():
        candidates = sorted(export_dir.glob("*.onnx"))
        if len(candidates) == 1:
            src = candidates[0]
        else:
            sys.exit(f"FATAL: export produced no file at {src} "
                     f"(found: {[c.name for c in candidates]})")
    return src


def read_onnx_io(path: Path) -> Tuple[List[Tuple[str, List[Any]]], List[Tuple[str, List[Any]]]]:
    """(inputs, outputs) as [(name, shape)] via onnxruntime, else onnx shape
    inference. Raises if neither can read the graph."""
    try:
        import onnxruntime as ort

        sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        return ([(i.name, list(i.shape)) for i in sess.get_inputs()],
                [(o.name, list(o.shape)) for o in sess.get_outputs()])
    except Exception as e:  # noqa: BLE001 - fall through to onnx
        print(f"WARN: onnxruntime could not open {path.name} ({e}); using onnx", flush=True)
    import onnx
    from onnx import shape_inference

    model = shape_inference.infer_shapes(onnx.load(str(path)))

    def dims(value_info):
        out = []
        for d in value_info.type.tensor_type.shape.dim:
            out.append(int(d.dim_value) if d.HasField("dim_value") else (d.dim_param or None))
        return out

    return ([(i.name, dims(i)) for i in model.graph.input],
            [(o.name, dims(o)) for o in model.graph.output])


def _as_shape(item) -> Tuple[Optional[str], List[Any]]:
    """Accept a bare shape or a (name, shape) pair."""
    if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str) \
            and isinstance(item[1], (list, tuple)):
        return item[0], list(item[1])
    return None, list(item)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def verify_two_outputs(outputs: Sequence[Any], num_classes: int) -> Tuple[int, List[int], List[int]]:
    """Check the exported graph is the two-tensor RF-DETR contract (pure).

    `outputs` is a list of shapes, or of (name, shape) pairs as read_onnx_io
    returns. Exactly two rank-3 outputs, both batch 1 with the same Q; one has
    last dim 4 (boxes, cxcywh); the other has last dim EXACTLY num_classes + 1
    (rfdetr 1.10.1 always allocates a trailing background slot). Names
    ("dets" / "labels") settle the ambiguity when num_classes + 1 == 4.
    Returns (Q, boxes_shape, logits_shape); raises ValueError with a FATAL
    message quoting the shapes otherwise (Req 1.6).

    A logits tensor with only num_classes slots is rejected too, with a
    message naming the missing background slot: it is what an un-widened head
    exports (a base checkpoint with fewer classes than the manifest, trained
    without the num_classes pin -- spike job tl13-rfdetr-nc2-0603 shipped
    labels [1,300,2] for a 2-class manifest), so the artifact would be
    mislabelled on device (Req 7.4).
    """
    parsed = [_as_shape(o) for o in outputs]
    shapes = [s for _n, s in parsed]
    quoted = ", ".join(f"{n or '?'}={s}" for n, s in parsed)
    if len(parsed) != 2:
        raise ValueError(f"FATAL: expected exactly 2 ONNX outputs ([1, Q, 4] boxes + "
                         f"[1, Q, num_classes + 1] logits), got {len(parsed)}: [{quoted}]")
    for _n, s in parsed:
        if len(s) != 3 or not all(_is_int(d) for d in s):
            raise ValueError(f"FATAL: ONNX outputs must be static rank-3 tensors, got [{quoted}]")
        if s[0] != 1:
            raise ValueError(f"FATAL: ONNX outputs must have batch 1, got [{quoted}]")
    if shapes[0][1] != shapes[1][1]:
        raise ValueError(f"FATAL: ONNX outputs disagree on Q (num_queries): [{quoted}]")

    num_classes = int(num_classes)
    expected_logits = num_classes + 1
    names = [n for n, _s in parsed]
    boxes = logits = None
    if ONNX_BOXES_NAME in names and ONNX_LOGITS_NAME in names:
        boxes = shapes[names.index(ONNX_BOXES_NAME)]
        logits = shapes[names.index(ONNX_LOGITS_NAME)]
    else:
        for s in shapes:
            other = shapes[1] if s is shapes[0] else shapes[0]
            if s[2] == 4 and other[2] != 4:
                boxes, logits = s, other
                break
        if boxes is None and shapes[0][2] == 4 and shapes[1][2] == 4:
            # Unnamed and both 4 wide: only valid when num_classes == 3, and
            # then the order is undecidable -- take (boxes, logits) as given.
            boxes, logits = shapes[0], shapes[1]
    if boxes is None or logits is None or boxes[2] != 4:
        raise ValueError(
            f"FATAL: ONNX output shapes [{quoted}] do not match [1, Q, 4] + "
            f"[1, Q, {expected_logits}] (num_classes={num_classes} + 1 background slot)")
    if logits[2] == num_classes:
        raise ValueError(
            f"FATAL: ONNX logits output {logits} has only num_classes={num_classes} slots; "
            f"expected [1, {logits[1]}, {expected_logits}] (num_classes + 1 background slot). "
            f"The detection head was not widened to the manifest's classes -- got "
            f"[{quoted}], expected [1, {logits[1]}, 4] + [1, {logits[1]}, {expected_logits}]")
    if logits[2] != expected_logits:
        raise ValueError(
            f"FATAL: ONNX output shapes [{quoted}] do not match [1, Q, 4] + "
            f"[1, Q, {expected_logits}] (num_classes={num_classes} + 1 background slot)")
    return int(boxes[1]), [int(d) for d in boxes], [int(d) for d in logits]


def verify_input(inputs: Sequence[Any], resolution: int) -> List[int]:
    """The single input must be static [1, 3, R, R] (pure; Req 1.6)."""
    parsed = [_as_shape(i) for i in inputs]
    quoted = ", ".join(f"{n or '?'}={s}" for n, s in parsed)
    if len(parsed) != 1:
        raise ValueError(f"FATAL: expected exactly 1 ONNX input, got [{quoted}]")
    shape = parsed[0][1]
    if shape != [1, 3, int(resolution), int(resolution)]:
        raise ValueError(f"FATAL: ONNX input {quoted} is not [1, 3, {resolution}, {resolution}]")
    return [int(d) for d in shape]


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def build_metadata(cfg, class_names: List[str], metrics: Dict[str, float],
                   input_shape: List[int], boxes_shape: List[int], logits_shape: List[int],
                   top_k: int, base: Optional[Path] = None) -> Dict[str, Any]:
    """training_metadata.json for an RF-DETR artifact (pure; Req 1.7).

    Mirrors train.py's keys where they overlap (epochs, opset, metrics,
    base_weights[_member], manifest_s3, images_s3, device_manifest_hints) and
    adds the RF-DETR ones the packager reads: detection_arch, rfdetr_size,
    resolution, num_classes, class_names, onnx_output_shapes, top_k.
    """
    num_classes = len(class_names)
    meta: Dict[str, Any] = {
        "detection_arch": "rf_detr",
        "rfdetr_size": cfg.size,
        "model_class": cfg.model_class,
        "resolution": cfg.resolution,
        "num_classes": num_classes,
        "class_names": list(class_names),
        "epochs": cfg.epochs,
        "batch": cfg.batch,
        "grad_accum": cfg.grad_accum,
        "lr": cfg.lr,
        "patience": cfg.patience,
        "opset": cfg.opset,
        "onnx_opset": cfg.opset,
        "onnx_input_shape": list(input_shape),
        # [boxes, logits]; logits is [1, Q, C + 1] -- the trailing slot is
        # rfdetr's never-positive background slot, class i == class_names[i].
        # verify_two_outputs has already enforced C + 1, so the background
        # slot index is always the last one.
        "onnx_output_shapes": [list(boxes_shape), list(logits_shape)],
        "logits_slots": int(logits_shape[2]),
        "background_slot": int(logits_shape[2]) - 1,
        "top_k": int(top_k),
        # The published size name, or the S3 URI the run was fine-tuned from
        # when BASE_WEIGHTS_S3 was set (base_weights_member then names the
        # checkpoint inside that artifact).
        "base_weights": cfg.base_weights_s3 if base is not None else cfg.model_class,
        "metrics": dict(metrics),
        "manifest_s3": cfg.manifest_s3,
        "images_s3": cfg.images_s3,
        # The device MUST square-resize (no letterbox) and ImageNet-normalise
        # to match how this was trained; top_k set-selection, no NMS.
        "device_manifest_hints": {
            "layout": "rf_detr",
            "stage_type": STAGE_TYPE,
            "network_input": cfg.resolution,
            "preserve_aspect": False,
            "normalize": True,
            "score_threshold": DEVICE_SCORE_THRESHOLD,
            "top_k": int(top_k),
        },
    }
    if base is not None:
        meta["base_weights_member"] = cfg.base_weights_member or CHECKPOINT_MEMBER
    return meta


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cfg = read_config()
    if not cfg.manifest_s3:
        sys.exit("FATAL: MANIFEST_S3 is required")
    print(f"MANIFEST_S3={cfg.manifest_s3}\nIMAGES_S3={cfg.images_s3 or '(from source-ref)'}\n"
          f"RFDETR_SIZE={cfg.size} ({cfg.model_class}) RESOLUTION={cfg.resolution} "
          f"EPOCHS={cfg.epochs} BATCH={cfg.batch} GRAD_ACCUM={cfg.grad_accum} "
          f"LR={cfg.lr} PATIENCE={cfg.patience} ONNX_OPSET={cfg.opset}", flush=True)
    if cfg.base_weights_s3:
        print(f"BASE_WEIGHTS_S3={cfg.base_weights_s3} "
              f"BASE_WEIGHTS_MEMBER={cfg.base_weights_member or CHECKPOINT_MEMBER}",
              flush=True)

    dataset, class_names = stage_dataset(cfg)
    # None unless BASE_WEIGHTS_S3 is set -> published COCO weights as before.
    base = fetch_base_weights(WORK / "base_weights", cfg.base_weights_s3,
                              cfg.base_weights_member, default_member=CHECKPOINT_MEMBER)
    if base is not None:
        log_checkpoint_classes(base, class_names)

    Model = model_class(cfg)
    model = build_model(Model, cfg, base, num_classes=len(class_names))
    best = train_model(model, cfg, dataset, class_names)

    # Everything shipped comes from the best checkpoint, reloaded from disk:
    # metrics, ONNX and the checkpoint copy then describe the same weights.
    best_model = reload_best(Model, best, cfg)
    metrics = read_test_metrics(best_model, dataset, cfg)

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    produced = export_onnx(best_model, cfg, WORK / "export")
    dst = MODEL_DIR / "model.onnx"
    shutil.copy2(produced, dst)
    print(f"exported {produced} -> {dst} ({dst.stat().st_size} bytes)", flush=True)
    # Keep the IR the edge runtimes load, whatever onnx the export used.
    cap_onnx_ir_version(dst)

    try:
        inputs, outputs = read_onnx_io(dst)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"FATAL: could not introspect exported ONNX {dst}: {e}")
    print(f"ONNX inputs : {inputs}\nONNX outputs: {outputs}", flush=True)
    try:
        input_shape = verify_input(inputs, cfg.resolution)
        top_k, boxes_shape, logits_shape = verify_two_outputs(outputs, len(class_names))
    except ValueError as e:
        sys.exit(str(e))
    print(f"ONNX contract OK: boxes {boxes_shape} logits {logits_shape} "
          f"(Q={top_k}, num_classes={len(class_names)} + 1 background slot)",
          flush=True)
    if top_k != TOP_K:
        print(f"WARN: Q={top_k} differs from the expected {TOP_K} queries; "
              f"top_k in the metadata follows the graph", flush=True)

    meta = build_metadata(cfg, class_names, metrics, input_shape, boxes_shape,
                          logits_shape, top_k, base)
    write_metadata(MODEL_DIR, meta)

    # Keep the fine-tunable checkpoint too: it is what a later run starts from
    # (BASE_WEIGHTS_S3 + BASE_WEIGHTS_MEMBER) and what you re-export from if
    # the resolution or opset needs changing (Req 1.7 / 1.9).
    shutil.copy2(best, MODEL_DIR / CHECKPOINT_MEMBER)
    print(f"kept {best} -> {MODEL_DIR / CHECKPOINT_MEMBER}", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
