#!/usr/bin/env python3
"""Apply the ``dda-telemetry/`` lifecycle rules to a Results_Bucket
(workflow-telemetry-dashboards design §12.4; workflow-debug-mode design §1.3,
runbook step RB-5).

    telemetry_lifecycle.py --bucket <name> [--clip-days 90] [--debug-days 7] [--apply]

Runs with the operator's own credentials (the Portal holds no
``s3:PutLifecycleConfiguration`` grant). Reads the bucket's lifecycle
configuration (a missing configuration is an empty rule list), merges the
three rule ids ``DDATelemetryClips``, ``DDATelemetryLive`` and
``DDATelemetryDebugImages`` through ``telemetry.retention.merge_telemetry_rules``
(every other rule is kept verbatim) and prints the merged rules. Nothing is
written without ``--apply``; with it, exactly one
``PutBucketLifecycleConfiguration`` carries the merged rules, and none when the
bucket already carries them. ``--debug-days`` accepts 1 to 30.
"""
import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

_HERE = os.path.dirname(os.path.abspath(__file__))
_FUNCTIONS = os.path.normpath(os.path.join(_HERE, "..", "backend", "functions"))
if _FUNCTIONS not in sys.path:
    sys.path.insert(0, _FUNCTIONS)

from telemetry import retention  # noqa: E402

__all__ = ["main", "read_rules", "parse_args"]


def _debug_days(value: str) -> int:
    low, high = retention.DEBUG_DAYS_RANGE
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--debug-days must be an integer")
    if not low <= days <= high:
        raise argparse.ArgumentTypeError("--debug-days must be between {0} and {1}".format(low, high))
    return days


def _clip_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--clip-days must be an integer")
    if days < 1:
        raise argparse.ArgumentTypeError("--clip-days must be at least 1")
    return days


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="telemetry_lifecycle.py",
        description="Merge the three dda-telemetry/ lifecycle rules into a Results_Bucket "
                    "(dry run unless --apply is given).")
    parser.add_argument("--bucket", required=True, help="the Results_Bucket name")
    parser.add_argument("--clip-days", type=_clip_days, default=retention.DEFAULT_CLIP_DAYS,
                        help="DDATelemetryClips expiration in days (default %(default)s)")
    parser.add_argument("--debug-days", type=_debug_days, default=retention.DEFAULT_DEBUG_DAYS,
                        help="DDATelemetryDebugImages expiration in days, 1-30 (default %(default)s)")
    parser.add_argument("--apply", action="store_true",
                        help="write the merged rules; without it nothing is changed")
    parser.add_argument("--region", default=None, help="the bucket's region (optional)")
    return parser.parse_args(argv)


def read_rules(s3: Any, bucket: str) -> List[Dict[str, Any]]:
    """The bucket's current lifecycle rules; a missing configuration is []."""
    try:
        response = s3.get_bucket_lifecycle_configuration(Bucket=bucket)
    except Exception as error:  # noqa: BLE001 - botocore ClientError without importing botocore here
        code = ""
        response_meta = getattr(error, "response", None)
        if isinstance(response_meta, dict):
            code = str((response_meta.get("Error") or {}).get("Code") or "")
        if code == "NoSuchLifecycleConfiguration":
            return []
        raise
    return list(response.get("Rules") or [])


def main(argv: Optional[Sequence[str]] = None, s3_client: Any = None, out=None) -> int:
    args = parse_args(argv)
    out = out or sys.stdout
    s3 = s3_client
    if s3 is None:
        import boto3  # noqa: WPS433 - operator credentials, outside the functions tree
        s3 = boto3.client("s3", region_name=args.region) if args.region else boto3.client("s3")
    existing = read_rules(s3, args.bucket)
    merged, changed = retention.merge_telemetry_rules(
        existing, clip_days=args.clip_days, debug_days=args.debug_days)
    print("Bucket: {0}".format(args.bucket), file=out)
    print("Existing rules: {0}; merged rules: {1}; change needed: {2}".format(
        len(existing), len(merged), "yes" if changed else "no"), file=out)
    print(json.dumps(merged, indent=2, sort_keys=True), file=out)
    if not args.apply:
        print("Dry run: nothing written (pass --apply to write the merged rules).", file=out)
        return 0
    if not changed:
        print("The bucket already carries these rules; nothing written.", file=out)
        return 0
    s3.put_bucket_lifecycle_configuration(
        Bucket=args.bucket, LifecycleConfiguration={"Rules": merged})
    print("Applied {0} rules to {1}.".format(len(merged), args.bucket), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
