#!/usr/bin/env python3
"""Read-only GPU quota audit for the hosting comparison (Req 3.4).

Queries AWS Service Quotas in us-east-1 for:
- EC2 running on-demand G/P/VT instance vCPU quotas
- SageMaker per-instance-type endpoint usage quotas for the GPU instance
  families the exploration cares about (g5, g6, g6e, p4d)

Outputs a markdown table (stdout, or --out FILE) consumed by
`artifacts/hosting-comparison.md` (task 7.1).

Strictly read-only: only `service_quotas:ListServiceQuotas` /
`GetServiceQuota` calls. Not Benchmark_Infrastructure; creates no resources.

Usage: python3 quota_audit.py [--out quota-audit.md]
"""

import argparse
import sys
from datetime import datetime, timezone

import boto3

REGION = "us-east-1"

# EC2 running-on-demand vCPU quota codes (account-level, per instance family group)
EC2_QUOTAS = [
    ("L-DB2E81BA", "Running On-Demand G and VT instances (vCPUs)"),
    ("L-417A185B", "Running On-Demand P instances (vCPUs)"),
]

# SageMaker endpoint-usage quotas are per instance type; match by quota name.
SAGEMAKER_INSTANCE_TYPES = [
    "ml.g5.xlarge",
    "ml.g6.xlarge",
    "ml.g6e.xlarge",
    "ml.g6e.2xlarge",
    "ml.g6e.4xlarge",
    "ml.p4d.24xlarge",
]


def audit_ec2(client) -> list:
    rows = []
    for code, label in EC2_QUOTAS:
        try:
            q = client.get_service_quota(ServiceCode="ec2", QuotaCode=code)["Quota"]
            rows.append(("ec2", label, q["QuotaCode"], q["Value"]))
        except Exception as exc:  # noqa: BLE001 — audit continues per-quota
            rows.append(("ec2", label, code, f"ERROR: {exc}"))
    return rows


def audit_sagemaker(client) -> list:
    """Collect endpoint-usage quotas for the instance types of interest."""
    wanted = {f"{t} for endpoint usage": t for t in SAGEMAKER_INSTANCE_TYPES}
    rows = []
    found = {}
    paginator = client.get_paginator("list_service_quotas")
    try:
        for page in paginator.paginate(ServiceCode="sagemaker"):
            for q in page["Quotas"]:
                if q["QuotaName"] in wanted:
                    found[q["QuotaName"]] = q
    except Exception as exc:  # noqa: BLE001
        rows.append(("sagemaker", "list_service_quotas", "-", f"ERROR: {exc}"))
        return rows
    for name, itype in wanted.items():
        q = found.get(name)
        if q:
            rows.append(("sagemaker", name, q["QuotaCode"], q["Value"]))
        else:
            rows.append(("sagemaker", name, "-", "not found (default may apply)"))
    return rows


def to_markdown(rows: list) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"# GPU Quota Audit — Portal_Account 164152369890, {REGION}",
        "",
        f"Captured {ts} by `benchmark-harness/quota_audit.py` (read-only, Req 3.4).",
        "",
        "| Service | Quota | Code | Current value |",
        "|---|---|---|---|",
    ]
    for service, label, code, value in rows:
        lines.append(f"| {service} | {label} | {code} | {value} |")
    lines += [
        "",
        "Interpretation for the hosting comparison (task 7.2): the EC2 G/VT",
        "vCPU quota bounds concurrent g5/g6/g6e benchmark and future always-on",
        "instances (g6e.xlarge = 4 vCPUs, g6e.2xlarge = 8, g6e.4xlarge = 16);",
        "the P quota bounds any p4d fallback (96 vCPUs per p4d.24xlarge).",
        "SageMaker per-instance-type endpoint quotas bound future endpoint",
        "hosting; zero/absent values need quota increase requests before a",
        "production implementation.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="write markdown here instead of stdout")
    args = ap.parse_args()

    client = boto3.client("service-quotas", region_name=REGION)
    rows = audit_ec2(client) + audit_sagemaker(client)
    md = to_markdown(rows)
    if args.out:
        with open(args.out, "w") as f:
            f.write(md)
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        print(md)


if __name__ == "__main__":
    main()
