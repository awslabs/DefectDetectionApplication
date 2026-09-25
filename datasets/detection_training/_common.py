#!/usr/bin/env python3
"""Helpers shared by the detection-training SageMaker entry points.

`train.py` (YOLO) and `train_rfdetr.py` (RF-DETR) are siblings in a FLAT
sourcedir.tar.gz, and so is this module: import it as `from _common import
...`, never as a package. Everything in here is trainer-agnostic:

  * `hp()`                     -- hyperparameter/env parsing
  * `stage_manifest_and_images` -- MANIFEST_S3 download + image download
                                  (IMAGES_S3 prefix or the manifest's own
                                  source-ref URIs)
  * `run_converter`            -- the bundled manifest_to_detector_dataset.py
  * `fetch_base_weights`       -- BASE_WEIGHTS_S3 / BASE_WEIGHTS_MEMBER -> a
                                  local checkpoint to fine-tune from
  * `write_metadata`           -- training_metadata.json

Nothing here imports torch / ultralytics / rfdetr: the module must import on
a bare host (and in the portal's unit tests) with only boto3 available, and
boto3 itself is imported lazily inside the functions that talk to S3.

Fatal conditions call `sys.exit("FATAL: ...")`, the convention the portal's
Training Detail page surfaces as the job's failure reason.
"""
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

MODEL_DIR = Path(os.environ.get("SM_MODEL_DIR", "/opt/ml/model"))
WORK = Path("/opt/ml/input/work")
# The converter sits beside this file in the flat sourcedir.
CODE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
CONVERTER = CODE_DIR / "manifest_to_detector_dataset.py"

IMAGE_EXTS = (".jpg", ".jpeg", ".png")
TARBALL_SUFFIXES = (".tar.gz", ".tgz", ".tar")
_GZIP_MAGIC = b"\x1f\x8b"


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def hp(name, default=None, cast=None):
    """Read a hyperparameter from the environment (SageMaker exports them).

    `cast` (e.g. `int`, `float`) is applied to the resolved value unless it is
    None, so `hp("EPOCHS", "100", int)` == `int(hp("EPOCHS", "100"))`.
    """
    value = os.environ.get(name, default)
    if cast is not None and value is not None:
        return cast(value)
    return value


def sh(cmd, **kw):
    """Run a command, echoing it first; returns the CompletedProcess."""
    print("+ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=False, **kw)


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------

def split_s3(uri: str) -> Tuple[str, str]:
    """s3://bucket/key -> (bucket, key)"""
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    return bucket, key


def s3_client():
    """A boto3 S3 client.

    boto3 rather than the aws CLI: boto3 is present in the DLC (the SageMaker
    training toolkit depends on it), the CLI is not guaranteed.
    """
    import boto3

    return boto3.client("s3")


def download_prefix(s3, images_s3: str, images: Path) -> int:
    """Download every image under an S3 prefix (IMAGES_S3 override path)."""
    # Trailing slash matters: without it, S3 prefix matching is plain string
    # matching and would also pull a sibling prefix sharing the same name
    # (e.g. `<name>-other-resolutions/`).
    ib, ik = split_s3(images_s3)
    if ik and not ik.endswith("/"):
        ik += "/"
    n_img = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=ib, Prefix=ik):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or not key.lower().endswith(IMAGE_EXTS):
                continue
            s3.download_file(ib, key, str(images / key.rsplit("/", 1)[-1]))
            n_img += 1
    return n_img


def download_source_refs(s3, manifest_lines: Iterable[str], images: Path) -> int:
    """Download the image behind every manifest `source-ref` (portal path).

    The converter keys images by basename, so two source-refs sharing a
    basename under different prefixes would silently overwrite each other;
    warn loudly instead of guessing.
    """
    seen = {}
    n_img = 0
    for i, line in enumerate(manifest_lines, 1):
        try:
            entry = json.loads(line)
        except ValueError:
            print(f"WARN: manifest line {i} is not JSON; skipped", flush=True)
            continue
        ref = entry.get("source-ref")
        if not isinstance(ref, str) or not ref.startswith("s3://"):
            print(f"WARN: manifest line {i} has no s3:// source-ref; skipped",
                  flush=True)
            continue
        name = ref.rsplit("/", 1)[-1]
        if name in seen:
            if seen[name] != ref:
                print(f"WARN: basename collision {name!r}: {seen[name]} vs {ref}; "
                      f"keeping the first", flush=True)
            continue
        seen[name] = ref
        b, k = split_s3(ref)
        try:
            s3.download_file(b, k, str(images / name))
        except Exception as e:
            print(f"WARN: could not download {ref}: {e}", flush=True)
            continue
        n_img += 1
    return n_img


def stage_manifest_and_images(
    manifest_s3: str,
    images_s3: Optional[str] = None,
    work: Path = WORK,
    s3=None,
) -> Tuple[Path, Path]:
    """Download the manifest and its images into `work`; returns
    (manifest_path, images_dir).

    With `images_s3` set, every image under that prefix is pulled (the manual
    launch path). Without it -- how the portal launches -- the manifest's own
    `source-ref` URIs are the authoritative image list, so exactly those are
    downloaded. Exits FATAL if the manifest cannot be fetched or no image
    arrives.
    """
    if s3 is None:
        s3 = s3_client()
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    manifest = work / "output.manifest"
    images = work / "images"
    images.mkdir(parents=True, exist_ok=True)

    mb, mk = split_s3(manifest_s3)
    try:
        s3.download_file(mb, mk, str(manifest))
    except Exception as e:
        sys.exit(f"FATAL: could not download manifest {manifest_s3}: {e}")
    manifest_lines = [ln for ln in manifest.read_text().splitlines() if ln.strip()]
    print(f"manifest lines: {len(manifest_lines)}", flush=True)

    if images_s3:
        n_img = download_prefix(s3, images_s3, images)
        where = images_s3
    else:
        # Portal launches set only MANIFEST_S3: the manifest's own source-ref
        # URIs are the authoritative image list, so pull exactly those.
        n_img = download_source_refs(s3, manifest_lines, images)
        where = f"{manifest_s3} (source-ref)"
    print(f"images downloaded: {n_img}", flush=True)
    if n_img == 0:
        sys.exit(f"FATAL: no images downloaded from {where}")
    return manifest, images


# ---------------------------------------------------------------------------
# Dataset conversion
# ---------------------------------------------------------------------------

def converter_command(
    manifest: Path,
    images_dir: Path,
    out_dir: Path,
    fmt: str = "yolo",
    coco_layout: Optional[str] = None,
    net_input_height: Optional[int] = None,
    extra_args: Sequence[str] = (),
) -> List[str]:
    """The manifest_to_detector_dataset.py command line (pure; for tests)."""
    cmd = [sys.executable, str(CONVERTER),
           "--manifest", str(manifest),
           "--images-dir", str(images_dir),
           "--out", str(out_dir),
           "--format", fmt]
    if coco_layout:
        cmd += ["--coco-layout", coco_layout]
    if net_input_height is not None:
        cmd += ["--net-input-height", str(net_input_height)]
    cmd += [str(a) for a in extra_args]
    return cmd


def run_converter(
    manifest: Path,
    images_dir: Path,
    out_dir: Path,
    fmt: str = "yolo",
    coco_layout: Optional[str] = None,
    net_input_height: Optional[int] = None,
    extra_args: Sequence[str] = (),
) -> Path:
    """Build the dataset with the bundled converter; returns `out_dir`.

    YOLO:    run_converter(m, imgs, out, "yolo", net_input_height=IMGSZ)
    RF-DETR: run_converter(m, imgs, out, "coco", coco_layout="rfdetr")
    Exits FATAL when the converter fails.
    """
    r = sh(converter_command(manifest, images_dir, out_dir, fmt,
                             coco_layout=coco_layout,
                             net_input_height=net_input_height,
                             extra_args=extra_args))
    if r.returncode != 0:
        sys.exit("FATAL: dataset conversion failed")
    return Path(out_dir)


# ---------------------------------------------------------------------------
# Base weights (transfer learning)
# ---------------------------------------------------------------------------

def _normalise_member(name: str) -> str:
    return name[2:] if name.startswith("./") else name


def _is_tarball(path: Path) -> bool:
    if path.name.lower().endswith(TARBALL_SUFFIXES):
        return True
    try:
        with open(path, "rb") as fh:
            head = fh.read(2)
    except OSError:
        return False
    return head == _GZIP_MAGIC and tarfile.is_tarfile(path)


def _extract_member(tarball: Path, member: str, dest_dir: Path) -> Path:
    """Extract exactly `member` from `tarball` into `dest_dir` (by basename).

    Matches the member by its full (`./`-stripped) name first, then by
    basename anywhere in the archive; a missing member is FATAL and the
    message lists what the archive does contain so the launcher can be
    fixed without another download.
    """
    with tarfile.open(tarball, "r:*") as tar:
        files = [m for m in tar.getmembers() if m.isfile()]
        by_name = {_normalise_member(m.name): m for m in files}
        hit = by_name.get(_normalise_member(member))
        if hit is None:
            same_base = [m for m in files
                         if Path(m.name).name == Path(member).name]
            if len(same_base) == 1:
                hit = same_base[0]
        if hit is None:
            listing = ", ".join(sorted(by_name)) or "(empty)"
            sys.exit(f"FATAL: BASE_WEIGHTS_MEMBER {member!r} not found in "
                     f"{tarball.name}; members: {listing}")
        dest = dest_dir / Path(hit.name).name
        # extractfile + copy rather than tar.extract: no path components from
        # the archive ever touch the filesystem.
        src = tar.extractfile(hit)
        if src is None:
            sys.exit(f"FATAL: could not read {member!r} from {tarball.name}")
        with src, open(dest, "wb") as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
    return dest


def fetch_base_weights(
    dest_dir: Path,
    base_weights_s3: Optional[str] = None,
    member: Optional[str] = None,
    default_member: Optional[str] = None,
    s3=None,
) -> Optional[Path]:
    """Resolve BASE_WEIGHTS_S3 (+ BASE_WEIGHTS_MEMBER) to a local checkpoint.

    Returns None when no base weights are configured -- the caller then uses
    the arch's published checkpoint, exactly as before this feature existed.

    Otherwise downloads the object into `dest_dir`:
      * a tarball (`.tar.gz` / `.tgz` / `.tar`, or gzip-tar by content -- a
        prior job's model.tar.gz) has the checkpoint `member` extracted from
        it (BASE_WEIGHTS_MEMBER, else `default_member`, e.g. `best.pt`);
        a missing member is FATAL with the archive's listing in the message;
      * anything else is treated as a bare weights file.
    `base_weights_s3` / `member` default to the environment; pass them (and an
    `s3` stub) explicitly in tests.
    """
    if base_weights_s3 is None:
        base_weights_s3 = hp("BASE_WEIGHTS_S3")
    if member is None:
        member = hp("BASE_WEIGHTS_MEMBER")
    base_weights_s3 = (base_weights_s3 or "").strip()
    member = (member or "").strip() or None
    if not base_weights_s3:
        return None
    if not base_weights_s3.startswith("s3://"):
        sys.exit(f"FATAL: BASE_WEIGHTS_S3 must be an s3:// URI, got {base_weights_s3!r}")

    if s3 is None:
        s3 = s3_client()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    bucket, key = split_s3(base_weights_s3)
    if not key or key.endswith("/"):
        sys.exit(f"FATAL: BASE_WEIGHTS_S3 must name an object, got {base_weights_s3!r}")
    local = dest_dir / key.rsplit("/", 1)[-1]
    print(f"base weights: downloading {base_weights_s3}"
          + (f" (member {member})" if member else ""), flush=True)
    try:
        s3.download_file(bucket, key, str(local))
    except Exception as e:
        sys.exit(f"FATAL: could not download base weights {base_weights_s3}: {e}")

    if _is_tarball(local):
        want = member or default_member
        if not want:
            with tarfile.open(local, "r:*") as tar:
                listing = ", ".join(sorted(_normalise_member(m.name)
                                           for m in tar.getmembers() if m.isfile()))
            sys.exit(f"FATAL: {base_weights_s3} is a tarball; set BASE_WEIGHTS_MEMBER "
                     f"to the checkpoint to extract (members: {listing or '(empty)'})")
        weights = _extract_member(local, want, dest_dir)
        try:
            local.unlink()
        except OSError:
            pass
    else:
        if member and Path(member).name != local.name:
            print(f"WARN: BASE_WEIGHTS_MEMBER={member!r} ignored: {base_weights_s3} "
                  f"is not a tarball", flush=True)
        weights = local
    print(f"base weights: {base_weights_s3} -> {weights} "
          f"({weights.stat().st_size} bytes)", flush=True)
    return weights


# ---------------------------------------------------------------------------
# ONNX IR version ceiling
# ---------------------------------------------------------------------------

#: Highest ONNX IR version the DDA edge runtimes load. Every model exported
#: before the onnx >= 1.22 bump carried IR 10 (what onnx 1.17 wrote), and
#: onnxruntime builds older than 1.20 (including the 1.19.2 pinned for the
#: in-job verification) reject anything newer. onnx 1.22 / 1.23 stamp IR
#: 13 / 14 on graphs they re-serialise, which ultralytics' onnxslim pass does.
EDGE_MAX_ONNX_IR_VERSION = 10


def cap_onnx_ir_version(path: Path, max_ir: int = EDGE_MAX_ONNX_IR_VERSION):
    """Lower an exported model's IR version to ``max_ir`` in place.

    The exported graph only uses opset-17 operators and standard tensor
    types, which IR 8 and later express identically, so lowering the IR
    stamp does not change the model; ``onnx.checker`` re-validates it
    under the lowered IR before the file is rewritten. Returns
    ``(before, after)``, or None when onnx is not importable (the file is
    then left untouched).
    """
    try:
        import onnx
    except ImportError:
        print("WARN: onnx is not importable; exported IR version left "
              "unchanged", flush=True)
        return None
    model = onnx.load(str(path))
    before = model.ir_version
    if before <= max_ir:
        return before, before
    model.ir_version = max_ir
    onnx.checker.check_model(model)
    onnx.save(model, str(path))
    print(f"ONNX IR version {before} -> {max_ir} (edge runtime ceiling)",
          flush=True)
    return before, max_ir


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def write_metadata(model_dir: Path, metadata: dict) -> Path:
    """Write `training_metadata.json` into `model_dir`; returns its path."""
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    path = model_dir / "training_metadata.json"
    path.write_text(json.dumps(metadata, indent=2))
    return path
