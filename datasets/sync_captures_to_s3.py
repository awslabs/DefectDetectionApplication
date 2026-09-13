#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Pull DDA device captures, dedupe them, and upload a clean detector dataset
to S3 in one pass.

Pipeline:
  1. Pull every non-overlay source frame from the device over ssh (tar
     stream, so one connection rather than one per file).
  2. Drop near-duplicate frames (delegates to dedupe_frames.py). A
     fixed-camera session produces runs where nothing moved; labeling those
     wastes money and inflates held-out metrics.
  3. Partition by capture resolution. The dominant resolution becomes the
     dataset; anything else is parked under a SIBLING prefix so a recursive
     listing for a labeling job cannot pick it up. Nothing is deleted.
  4. Upload with `aws s3 sync`, which is idempotent -- safe to re-run after
     shooting more frames, and it skips whatever is already uploaded.

Safe to run repeatedly. Each run reconsiders the full capture set, so newly
shot frames (e.g. a negatives session) are deduped against everything
already there.

Usage:
    # Full run
    python3 sync_captures_to_s3.py \\
        --bucket ryvan-cookies --prefix imts-plates-luggage

    # See what it would do, upload nothing
    python3 sync_captures_to_s3.py \\
        --bucket ryvan-cookies --prefix imts-plates-luggage --dry-run

    # Reuse frames already pulled, skip the ssh step
    python3 sync_captures_to_s3.py \\
        --bucket ryvan-cookies --prefix imts-plates-luggage --skip-pull

Requires: ssh access to the device, aws credentials, numpy, and Pillow or
OpenCV.
"""

import argparse
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dedupe_frames  # noqa: E402

DEFAULT_HOST = "nvidia@192.168.8.224"
DEFAULT_DEVICE_DIR = "/aws_dda/inference-results/u724uckx"


def run(cmd, **kw):
    """Run a command, streaming nothing, returning CompletedProcess."""
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def step(n, total, msg):
    print(f"\n[{n}/{total}] {msg}", flush=True)


def pull_frames(host, device_dir, dest):
    """Stream all non-overlay .jpg frames off the device into dest."""
    dest.mkdir(parents=True, exist_ok=True)
    remote = (
        f"cd {device_dir} && ls *.jpg 2>/dev/null "
        f"| grep -v overlay | tar cf - -T -"
    )
    print(f"  ssh {host} -> {dest}", flush=True)
    ssh = subprocess.Popen(
        ["ssh", "-o", "StrictHostKeyChecking=no", host, remote],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tar = subprocess.Popen(["tar", "xf", "-", "-C", str(dest)],
                           stdin=ssh.stdout, stderr=subprocess.PIPE)
    ssh.stdout.close()
    tar_err = tar.communicate()[1]
    ssh_err = ssh.stderr.read()
    ssh.wait()
    if tar.returncode != 0:
        print(f"  ERROR: tar failed: {tar_err.decode(errors='replace')[:800]}")
        return False
    if ssh.returncode != 0:
        print(f"  ERROR: ssh failed: {ssh_err.decode(errors='replace')[:800]}")
        return False
    n = len(list(dest.glob("*.jpg")))
    print(f"  pulled {n} frames", flush=True)
    return n > 0


def partition_by_resolution(kept):
    """Split [(path, (w, h))] into (dominant_res, main_list, odd_list)."""
    by_res = defaultdict(list)
    for path, size in kept:
        by_res[size].append(path)
    counts = Counter({k: len(v) for k, v in by_res.items()})
    dominant, _ = counts.most_common(1)[0]

    print("\n  resolution spread:", flush=True)
    for (w, h), n in counts.most_common():
        tag = "  <- DATASET" if (w, h) == dominant else "  (parked)"
        print(f"    {w}x{h:<6} aspect={w / h:5.2f}  frames={n:<4}{tag}",
              flush=True)

    main = by_res[dominant]
    odd = [p for k, v in by_res.items() if k != dominant for p in v]
    return dominant, main, odd


def stage(files, out_dir):
    """Copy files into a flat staging dir (resolves symlinks for s3 sync)."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in files:
        shutil.copy2(p.resolve(), out_dir / p.name)
    return out_dir


def s3_sync(src_dir, bucket, prefix, dry_run):
    dst = f"s3://{bucket}/{prefix}/"
    cmd = ["aws", "s3", "sync", str(src_dir) + "/", dst, "--only-show-errors"]
    if dry_run:
        cmd.append("--dryrun")
    print(f"  {' '.join(cmd[:4])} ...", flush=True)
    r = run(cmd)
    if r.returncode != 0:
        print(f"  UPLOAD FAILED rc={r.returncode}")
        print(f"  stdout: {r.stdout[-1500:]}")
        print(f"  stderr: {r.stderr[-1500:]}")
        return False
    if dry_run and r.stdout.strip():
        print("  (dryrun) " + r.stdout.strip()[:2000])
    print("  ok", flush=True)
    return True


def s3_count(bucket, prefix):
    """Object count under a prefix, or None if the call fails."""
    r = run(["aws", "s3", "ls", f"s3://{bucket}/{prefix}/", "--recursive"])
    if r.returncode != 0:
        return None
    return sum(1 for ln in r.stdout.splitlines()
               if ln.strip() and not ln.rstrip().endswith("/"))


def main():
    ap = argparse.ArgumentParser(
        description="Pull device captures, dedupe, and upload to S3.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", required=True,
                    help="Dataset prefix, e.g. imts-plates-luggage")
    ap.add_argument("--odd-prefix", default=None,
                    help="Prefix for off-resolution frames "
                         "(default: <prefix>-other-resolutions)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--device-dir", default=DEFAULT_DEVICE_DIR)
    ap.add_argument("--work-dir", default=".dda_capture_sync",
                    help="Local scratch dir (default: .dda_capture_sync)")
    ap.add_argument("--threshold", type=float,
                    default=dedupe_frames.DEFAULT_THRESHOLD,
                    help="Dedupe mean-abs-diff cutoff "
                         f"(default {dedupe_frames.DEFAULT_THRESHOLD})")
    ap.add_argument("--skip-pull", action="store_true",
                    help="Use frames already in <work-dir>/raw")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do everything except actually upload")
    args = ap.parse_args()

    odd_prefix = args.odd_prefix or f"{args.prefix}-other-resolutions"
    work = Path(args.work_dir)
    raw = work / "raw"
    total = 5

    print("=" * 68)
    print("DDA capture -> S3 dataset sync")
    print(f"  dataset : s3://{args.bucket}/{args.prefix}/")
    print(f"  parked  : s3://{args.bucket}/{odd_prefix}/")
    print("=" * 68)

    # 1. pull
    step(1, total, "Pull frames from device")
    if args.skip_pull:
        n = len(list(raw.glob("*.jpg"))) if raw.is_dir() else 0
        print(f"  --skip-pull: using {n} frames already in {raw}", flush=True)
        if not n:
            print("  ERROR: nothing there; drop --skip-pull")
            return 1
    elif not pull_frames(args.host, args.device_dir, raw):
        return 1

    # 2. dedupe
    step(2, total, "Drop near-duplicate frames")
    dedupe_frames.BACKEND = dedupe_frames._load_backend()
    paths = dedupe_frames.find_images(raw, ["overlay"])
    if not paths:
        print(f"  ERROR: no images under {raw}")
        return 1
    kept, dropped, distances = dedupe_frames.dedupe(paths, args.threshold)
    print(f"  kept {len(kept)} of {len(paths)} "
          f"({len(dropped)} near-duplicates dropped)", flush=True)
    if dropped:
        print("  dropped (nearest kept, distance):", flush=True)
        for path, _ in dropped[:40]:
            near, dist = distances[path]
            near_name = near.name if near is not None else "-"
            print(f"    {path.name[:44]:<44} ~ {near_name[:44]:<44} "
                  f"{dist:.2f}", flush=True)
        if len(dropped) > 40:
            print(f"    ... and {len(dropped) - 40} more", flush=True)

    # 3. partition
    step(3, total, "Partition by capture resolution")
    dominant, main_files, odd_files = partition_by_resolution(kept)
    w, h = dominant
    print(f"\n  dataset : {len(main_files)} frames at {w}x{h} "
          f"(aspect {w / h:.3f})", flush=True)
    print(f"  parked  : {len(odd_files)} frames at other resolutions",
          flush=True)
    stride_w, stride_h = (w // 32) * 32, (h // 32) * 32
    net_h = 1280
    net_w = int(round(net_h * w / h / 32)) * 32
    print(f"\n  Suggested ONNX export input for this framing: "
          f"{net_w}x{net_h}", flush=True)
    print(f"  (matches aspect {net_w / net_h:.3f} vs capture {w / h:.3f}; "
          f"both divisible by 32)", flush=True)

    # 4. stage
    step(4, total, "Stage files for upload")
    main_dir = stage(main_files, work / "stage_main")
    print(f"  staged {len(main_files)} -> {main_dir}", flush=True)
    odd_dir = None
    if odd_files:
        odd_dir = stage(odd_files, work / "stage_odd")
        print(f"  staged {len(odd_files)} -> {odd_dir}", flush=True)

    # 5. upload
    step(5, total, "Upload to S3" + (" (DRY RUN)" if args.dry_run else ""))
    if not s3_sync(main_dir, args.bucket, args.prefix, args.dry_run):
        return 1
    if odd_dir and not s3_sync(odd_dir, args.bucket, odd_prefix, args.dry_run):
        return 1

    print("\n" + "=" * 68)
    if args.dry_run:
        print("DRY RUN complete -- nothing uploaded.")
        return 0
    got_main = s3_count(args.bucket, args.prefix)
    got_odd = s3_count(args.bucket, odd_prefix)
    print("VERIFY (objects now in S3):")
    print(f"  s3://{args.bucket}/{args.prefix}/ "
          f"= {got_main}  (expected {len(main_files)})")
    print(f"  s3://{args.bucket}/{odd_prefix}/ "
          f"= {got_odd}  (expected {len(odd_files)})")
    ok = got_main == len(main_files) and got_odd == len(odd_files)
    print("\n" + ("ALL COUNTS MATCH" if ok else
                  "COUNT MISMATCH -- re-run to finish (sync is idempotent). "
                  "A stray zero-byte folder marker can also offset a count "
                  "by one; check with `aws s3 ls`."))
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
