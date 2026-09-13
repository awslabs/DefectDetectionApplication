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
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MODEL_DIR = Path(os.environ.get("SM_MODEL_DIR", "/opt/ml/model"))
WORK = Path("/opt/ml/input/work")
CODE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))


def hp(name, default=None):
    """Read a hyperparameter from the environment (SageMaker exports them)."""
    return os.environ.get(name, default)


MANIFEST_S3 = hp("MANIFEST_S3")
IMAGES_S3 = hp("IMAGES_S3")
IMGSZ = int(hp("IMGSZ", "1280"))
EPOCHS = int(hp("EPOCHS", "100"))
BATCH = int(hp("BATCH", "4"))
BASE_WEIGHTS = hp("BASE_WEIGHTS", "yolo11s.pt")
PATIENCE = int(hp("PATIENCE", "30"))
OPSET = int(hp("ONNX_OPSET", "17"))


def sh(cmd, **kw):
    print("+ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=False, **kw)


def _split_s3(uri):
    """s3://bucket/key -> (bucket, key)"""
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    return bucket, key


def stage_data():
    """Download manifest + images, then build the YOLO dataset.

    Uses boto3 rather than the aws CLI: boto3 is present in the DLC (the
    SageMaker training toolkit depends on it), the CLI is not guaranteed.
    """
    import boto3

    s3 = boto3.client("s3")
    WORK.mkdir(parents=True, exist_ok=True)
    manifest = WORK / "output.manifest"
    images = WORK / "images"
    images.mkdir(parents=True, exist_ok=True)

    mb, mk = _split_s3(MANIFEST_S3)
    try:
        s3.download_file(mb, mk, str(manifest))
    except Exception as e:
        sys.exit(f"FATAL: could not download manifest {MANIFEST_S3}: {e}")
    n_lines = sum(1 for ln in manifest.read_text().splitlines() if ln.strip())
    print(f"manifest lines: {n_lines}", flush=True)

    # Trailing slash matters: without it, S3 prefix matching is plain string
    # matching and would also pull a sibling prefix sharing the same name
    # (e.g. `<name>-other-resolutions/`).
    ib, ik = _split_s3(IMAGES_S3)
    if ik and not ik.endswith("/"):
        ik += "/"
    exts = (".jpg", ".jpeg", ".png")
    n_img = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=ib, Prefix=ik):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or not key.lower().endswith(exts):
                continue
            s3.download_file(ib, key, str(images / key.rsplit("/", 1)[-1]))
            n_img += 1
    print(f"images downloaded: {n_img}", flush=True)
    if n_img == 0:
        sys.exit(f"FATAL: no images downloaded from s3://{ib}/{ik}")

    dataset = WORK / "dataset"
    r = sh([sys.executable, str(CODE_DIR / "manifest_to_detector_dataset.py"),
            "--manifest", str(manifest),
            "--images-dir", str(images),
            "--out", str(dataset),
            "--format", "yolo",
            "--net-input-height", str(IMGSZ)])
    if r.returncode != 0:
        sys.exit("FATAL: dataset conversion failed")

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


def train(yaml_path):
    from ultralytics import YOLO

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


def export(model, metrics):
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
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
            print(f"channel dim = {ch} (expect 4 + num_classes = 5)", flush=True)
    except Exception as e:
        print(f"WARN: could not introspect exported ONNX: {e}", flush=True)

    meta = {
        "imgsz": IMGSZ,
        "base_weights": BASE_WEIGHTS,
        "epochs": EPOCHS,
        "opset": OPSET,
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
    (MODEL_DIR / "training_metadata.json").write_text(json.dumps(meta, indent=2))

    # Keep the ultralytics .pt too: it is what you re-export from if the
    # input size or opset needs changing later.
    best = WORK / "runs" / "train" / "weights" / "best.pt"
    if best.is_file():
        shutil.copy2(best, MODEL_DIR / "best.pt")
        print(f"kept {best} -> {MODEL_DIR / 'best.pt'}", flush=True)


def main():
    if not MANIFEST_S3 or not IMAGES_S3:
        sys.exit("FATAL: MANIFEST_S3 and IMAGES_S3 are required")
    print(f"MANIFEST_S3={MANIFEST_S3}\nIMAGES_S3={IMAGES_S3}\n"
          f"IMGSZ={IMGSZ} EPOCHS={EPOCHS} BATCH={BATCH} "
          f"BASE_WEIGHTS={BASE_WEIGHTS}", flush=True)
    _dataset, yaml_path = stage_data()
    model, _results = train(yaml_path)
    metrics = evaluate(model, yaml_path)
    export(model, metrics)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
