#!/usr/bin/env python3
"""SageMaker script-mode entry point: fine-tune a YOLO detector on a DDA
ObjectDetection manifest and export ONNX for the DDA edge runtime.

Runs entirely inside the training container:
  1. download the labeling job's output manifest from S3
  2. download the referenced images
  3. build a YOLO dataset via the bundled manifest_to_detector_dataset.py
     (same deterministic, leakage-safe split as the local tool)
  4. fine-tune from pretrained weights, letterboxed at a square imgsz
  5. export ONNX with NO in-graph NMS (the device's
     YoloDetectionPostProcessor does NMS) and verify the output shape
  6. write model.onnx + metadata to /opt/ml/model

Geometry contract: train letterboxed at IMGSZ square and export at the same
IMGSZ square. The device must then set preserve_aspect=true so it letterboxes
identically. Any mismatch here silently degrades detection.

Transfer learning: when BASE_WEIGHTS_S3 is set (a prior job's model.tar.gz
plus BASE_WEIGHTS_MEMBER=best.pt, or a bare .pt) the run starts from that
checkpoint instead of the published BASE_WEIGHTS; otherwise behaviour is
unchanged. ultralytics re-initialises the class head itself when the
manifest's class count differs from the checkpoint's ("Overriding model.yaml
nc=..."); this script only logs old vs new (_log_class_names) and records the
manifest's num_classes / class_names in training_metadata.json so the
artifact is self-describing without opening best.pt (Req 7.4).

ultralytics AutoUpdate is disabled before export (disable_autoupdate): the
exporter's check_requirements would otherwise pip-install an unpinned
onnxruntime-gpu inside the job. Everything the export needs is pinned in
requirements.txt.

The S3 / converter / metadata plumbing lives in the sibling _common.py
(shared with train_rfdetr.py); SageMaker extracts sourcedir.tar.gz flat, so
it is imported as a plain sibling module.
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

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

MANIFEST_S3 = hp("MANIFEST_S3")
IMAGES_S3 = hp("IMAGES_S3")
IMGSZ = hp("IMGSZ", "1280", int)
EPOCHS = hp("EPOCHS", "100", int)
BATCH = hp("BATCH", "4", int)
BASE_WEIGHTS = hp("BASE_WEIGHTS", "yolo11s.pt")
PATIENCE = hp("PATIENCE", "30", int)
OPSET = hp("ONNX_OPSET", "17", int)
BASE_WEIGHTS_S3 = hp("BASE_WEIGHTS_S3")
BASE_WEIGHTS_MEMBER = hp("BASE_WEIGHTS_MEMBER")
# The fine-tunable checkpoint this entry point leaves in its own artifact, and
# therefore the member to pull from a prior job's model.tar.gz when
# BASE_WEIGHTS_MEMBER is not given explicitly.
CHECKPOINT_MEMBER = "best.pt"

# ultralytics reads YOLO_AUTOINSTALL once, when `ultralytics.utils` is first
# imported, into the module constant AUTOINSTALL that check_requirements gates
# its pip installs on. Set it here, before any `from ultralytics import ...`
# (all of which are inside functions), so AutoUpdate is off for the whole
# job; disable_autoupdate() re-asserts it at runtime before export.
os.environ.setdefault("YOLO_AUTOINSTALL", "False")


def stage_data():
    """Download manifest + images, then build the YOLO dataset."""
    manifest, images = stage_manifest_and_images(MANIFEST_S3, IMAGES_S3, WORK)

    dataset = run_converter(manifest, images, WORK / "dataset", "yolo",
                            net_input_height=IMGSZ)

    yaml_path = dataset / "data.yaml"
    if not yaml_path.is_file():
        sys.exit("FATAL: converter produced no data.yaml")
    # Rewrite `path` to the in-container location (the converter writes an
    # absolute path from wherever it ran).
    text = yaml_path.read_text().splitlines()
    out = [f"path: {dataset}" if ln.startswith("path:") else ln for ln in text]
    yaml_path.write_text("\n".join(out) + "\n")
    print("--- data.yaml ---\n" + yaml_path.read_text(), flush=True)
    return dataset, yaml_path


def _manifest_class_names(yaml_path):
    """The `names:` block of the converter's data.yaml, in index order."""
    names = {}
    in_names = False
    for ln in Path(yaml_path).read_text().splitlines():
        if ln.startswith("names:"):
            in_names = True
            continue
        if not in_names:
            continue
        m = re.match(r"^\s+(\d+):\s*(.*?)\s*$", ln)
        if not m:
            break
        names[int(m.group(1))] = m.group(2)
    return [names[i] for i in sorted(names)]


def _log_class_names(model, yaml_path):
    """Log the base checkpoint's classes against the manifest's.

    ultralytics re-initialises the detection head itself when `nc` differs
    (it logs "Overriding model.yaml nc=..."); this just makes the old/new
    class lists visible in the job log so a re-init is never a surprise.
    """
    try:
        old = getattr(model, "names", None)
        if isinstance(old, dict):
            old = [old[k] for k in sorted(old)]
        old = list(old or [])
        new = _manifest_class_names(yaml_path)
        if old != new:
            print(f"base checkpoint classes ({len(old)}): {old}\n"
                  f"manifest classes ({len(new)}): {new}\n"
                  f"class set differs: the detection head will be re-initialised",
                  flush=True)
        else:
            print(f"base checkpoint classes match the manifest: {new}", flush=True)
    except Exception as e:
        print(f"WARN: could not compare class names: {e}", flush=True)


def train(yaml_path, base=None):
    from ultralytics import YOLO

    if base is not None:
        # Transfer learning from a prior job / imported checkpoint. No
        # fallback here: silently training from COCO instead of the requested
        # base would be worse than failing.
        weights = str(base)
        try:
            model = YOLO(weights)
        except Exception as e:
            sys.exit(f"FATAL: could not load base weights {weights} "
                     f"(from {BASE_WEIGHTS_S3}): {e}")
        print(f"base weights: {weights} (from {BASE_WEIGHTS_S3})", flush=True)
        _log_class_names(model, yaml_path)
    else:
        weights = BASE_WEIGHTS
        try:
            model = YOLO(weights)
        except Exception as e:
            print(f"WARN: {weights} unavailable ({e}); falling back to yolov8s.pt",
                  flush=True)
            weights = "yolov8s.pt"
            model = YOLO(weights)
        print(f"base weights: {weights}", flush=True)

    results = model.train(
        data=str(yaml_path),
        epochs=EPOCHS,
        imgsz=IMGSZ,
        batch=BATCH,
        patience=PATIENCE,
        project=str(WORK / "runs"),
        name="train",
        exist_ok=True,
        pretrained=True,
        val=True,
        plots=False,
        # Letterbox (rect=False -> square letterboxed input), matching how the
        # device will preprocess with preserve_aspect=true.
        rect=False,
        # This is a fixed-camera industrial scene: heavy geometric/colour
        # augmentation would model variation that never occurs. Keep flips and
        # mild scale/translate only.
        degrees=10.0, translate=0.10, scale=0.30, shear=0.0,
        perspective=0.0, flipud=0.5, fliplr=0.5,
        mosaic=0.5, mixup=0.0, copy_paste=0.0,
        hsv_h=0.015, hsv_s=0.4, hsv_v=0.4,
    )
    return model, results


def evaluate(model, yaml_path):
    metrics = {}
    try:
        val = model.val(data=str(yaml_path), imgsz=IMGSZ, split="test")
        box = getattr(val, "box", None)
        if box is not None:
            metrics = {
                "test_map50": float(getattr(box, "map50", float("nan"))),
                "test_map50_95": float(getattr(box, "map", float("nan"))),
                "test_precision": float(getattr(box, "mp", float("nan"))),
                "test_recall": float(getattr(box, "mr", float("nan"))),
            }
        print("TEST METRICS: " + json.dumps(metrics), flush=True)
    except Exception as e:
        print(f"WARN: test-split evaluation failed: {e}", flush=True)
    return metrics


def disable_autoupdate():
    """Turn ultralytics AutoUpdate off before export (Req 7.4).

    Exporter.export_onnx calls check_requirements(["onnx", "onnxslim",
    "onnxruntime-gpu"]) and, with AutoUpdate on, pip-installs whatever is
    missing -- on a CUDA host that is an unpinned ~250 MB onnxruntime-gpu on
    every job (spike 1.3 (a), (c-yolo)). Everything the export needs is pinned
    in requirements.txt, so with AutoUpdate off check_requirements simply
    returns False and the export proceeds.

    Three switches, most to least authoritative for ultralytics 8.3.40:
      1. YOLO_AUTOINSTALL=False in the environment (read at first import);
      2. the AUTOINSTALL constant in ultralytics.utils / .utils.checks (the
         name check_requirements actually tests) forced False, in case
         ultralytics was imported before this module set the env var;
      3. settings.update(autoinstall=False) -- the documented knob in the
         requirement; 8.3.40's SettingsManager has no such key and raises
         KeyError, which is logged and ignored (1. and 2. already hold).
    Never fatal: a failure here only means AutoUpdate may still be on.
    Returns which switches took effect (for the job log and tests).
    """
    applied = {"env": False, "constant": False, "settings": False}
    os.environ["YOLO_AUTOINSTALL"] = "False"
    applied["env"] = True
    try:
        import ultralytics.utils as ul_utils

        ul_utils.AUTOINSTALL = False
        try:
            import ultralytics.utils.checks as ul_checks

            ul_checks.AUTOINSTALL = False
        except Exception as e:  # noqa: BLE001
            print(f"WARN: could not force ultralytics.utils.checks.AUTOINSTALL: {e}", flush=True)
        applied["constant"] = True
    except Exception as e:  # noqa: BLE001
        print(f"WARN: could not force ultralytics AUTOINSTALL off: {e}", flush=True)
    try:
        from ultralytics import settings

        settings.update(autoinstall=False)
        applied["settings"] = True
    except Exception as e:  # noqa: BLE001
        print(f"note: ultralytics settings.update(autoinstall=False) not applied ({e}); "
              f"AutoUpdate is disabled via YOLO_AUTOINSTALL", flush=True)
    print(f"ultralytics AutoUpdate disabled: {applied}", flush=True)
    return applied


def build_metadata(metrics, shape, base=None, class_names=None):
    """training_metadata.json for a YOLO artifact (pure; Req 7.4).

    `class_names` is the converter's data.yaml `names` block in index order
    (_manifest_class_names), so `num_classes` / `class_names` describe the
    exported head without opening best.pt -- the same keys train_rfdetr.py
    writes. All pre-existing keys are unchanged.
    """
    names = [str(n) for n in (class_names or [])]
    meta = {
        "imgsz": IMGSZ,
        # The published checkpoint name, or the S3 URI the run was fine-tuned
        # from when BASE_WEIGHTS_S3 was set (base_weights_member then names
        # the checkpoint inside that artifact).
        "base_weights": BASE_WEIGHTS_S3 if base is not None else BASE_WEIGHTS,
        "epochs": EPOCHS,
        "opset": OPSET,
        "num_classes": len(names),
        "class_names": names,
        "onnx_output_shape": shape,
        "metrics": metrics,
        "manifest_s3": MANIFEST_S3,
        "images_s3": IMAGES_S3,
        # The device MUST letterbox to match how this was trained.
        "device_manifest_hints": {
            "preserve_aspect": True,
            "network_input": IMGSZ,
            "layout": "yolo",
            "iou_threshold": 0.45,
            "score_threshold": 0.25,
        },
    }
    if base is not None:
        meta["base_weights_member"] = BASE_WEIGHTS_MEMBER or CHECKPOINT_MEMBER
    return meta


def export(model, metrics, base=None, class_names=None):
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    # Before model.export(): its check_requirements would otherwise
    # pip-install an unpinned onnxruntime-gpu inside the job (Req 7.4).
    disable_autoupdate()
    path = model.export(
        format="onnx",
        imgsz=IMGSZ,
        opset=OPSET,
        dynamic=False,
        simplify=True,
        nms=False,          # device-side postprocessor does NMS
    )
    src = Path(str(path))
    if not src.is_file():
        sys.exit(f"FATAL: export produced no file at {src}")
    dst = MODEL_DIR / "model.onnx"
    shutil.copy2(src, dst)
    print(f"exported {src} -> {dst} ({dst.stat().st_size} bytes)", flush=True)
    # onnxslim (simplify=True) re-serialises the graph with the installed
    # onnx's IR version; keep the IR the edge runtimes load.
    cap_onnx_ir_version(dst)

    # Verify the graph matches what the device decoder expects: one input,
    # one output shaped [1, 4+nc, N]. A surprise here (e.g. NMS baked in, or
    # a transposed layout) is far cheaper to catch now than on device.
    shape = None
    try:
        import onnxruntime as ort
        sess = ort.InferenceSession(str(dst), providers=["CPUExecutionProvider"])
        ins = [(i.name, i.shape) for i in sess.get_inputs()]
        outs = [(o.name, o.shape) for o in sess.get_outputs()]
        print(f"ONNX inputs : {ins}", flush=True)
        print(f"ONNX outputs: {outs}", flush=True)
        if len(outs) != 1:
            print(f"WARN: expected 1 output, got {len(outs)} -- the DDA "
                  f"YoloDetectionPostProcessor reads output[0] only", flush=True)
        shape = outs[0][1] if outs else None
        if shape and len(shape) == 3:
            ch = min(int(shape[1]), int(shape[2]))
            # 4 box coords + one score per class; read nc from the manifest's
            # data.yaml (not a fixed 5) so a multi-class run reports the
            # right expectation (spike 1.3: a 2-class fine-tune printed
            # "6 (expect 5)" when this was hard-coded).
            if class_names:
                print(f"channel dim = {ch} (expect 4 + num_classes = "
                      f"{4 + len(class_names)})", flush=True)
            else:
                print(f"channel dim = {ch} (expect 4 + num_classes)", flush=True)
    except Exception as e:
        print(f"WARN: could not introspect exported ONNX: {e}", flush=True)

    write_metadata(MODEL_DIR, build_metadata(metrics, shape, base, class_names))

    # Keep the ultralytics .pt too: it is what you re-export from if the
    # input size or opset needs changing later.
    best = WORK / "runs" / "train" / "weights" / "best.pt"
    if best.is_file():
        shutil.copy2(best, MODEL_DIR / "best.pt")
        print(f"kept {best} -> {MODEL_DIR / 'best.pt'}", flush=True)


def main():
    if not MANIFEST_S3:
        sys.exit("FATAL: MANIFEST_S3 is required")
    print(f"MANIFEST_S3={MANIFEST_S3}\nIMAGES_S3={IMAGES_S3 or '(from source-ref)'}\n"
          f"IMGSZ={IMGSZ} EPOCHS={EPOCHS} BATCH={BATCH} "
          f"BASE_WEIGHTS={BASE_WEIGHTS}", flush=True)
    if BASE_WEIGHTS_S3:
        print(f"BASE_WEIGHTS_S3={BASE_WEIGHTS_S3} "
              f"BASE_WEIGHTS_MEMBER={BASE_WEIGHTS_MEMBER or CHECKPOINT_MEMBER}",
              flush=True)
    _dataset, yaml_path = stage_data()
    # None unless BASE_WEIGHTS_S3 is set -> published BASE_WEIGHTS as before.
    base = fetch_base_weights(WORK / "base_weights", BASE_WEIGHTS_S3,
                              BASE_WEIGHTS_MEMBER, default_member=CHECKPOINT_MEMBER)
    model, _results = train(yaml_path, base)
    metrics = evaluate(model, yaml_path)
    export(model, metrics, base, class_names=_manifest_class_names(yaml_path))
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
