"""Benchmark run core: case loop, metrics record, schema assertion.

Pure logic — no torch/diffusers imports here, so it is unit-testable on any
machine. The GPU-specific generation callable is injected (see run_driver.py).

Protocol references: §3 (per-run procedure), §4 (metrics schema),
Req 2.6/2.7 (result-record completeness, Property 4),
Req 2.10 (failure isolation, Property 5).
"""

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ACCOUNT = "164152369890"
REGION = "us-east-1"

RUN_REQUIRED_FIELDS = [
    "run_id", "model", "instance_type", "account", "region",
    "model_load_seconds", "cases", "instance_hours", "estimated_cost_usd",
    "billing_reconciled_cost_usd",
]
CASE_REQUIRED_FIELDS = [
    "case_id", "task_type", "latency_seconds", "seed", "output_uri",
    "status", "failure_mode",
]
VALID_STATUSES = {"ok", "failed"}
VALID_TASK_TYPES = {"inpainting", "text_to_image"}


class MetricsSchemaError(AssertionError):
    """Raised when a run record fails the completeness assertion."""


def load_cases(manifest_path: Path) -> List[Dict[str, Any]]:
    """Load the frozen cases.json manifest in order."""
    with open(manifest_path) as f:
        return json.load(f)


def run_cases(
    cases: List[Dict[str, Any]],
    generate: Callable[[Dict[str, Any]], str],
    clock: Callable[[], float] = time.monotonic,
) -> List[Dict[str, Any]]:
    """Execute every case in order; isolate per-case failures (Req 2.10).

    `generate(case)` produces one image and returns its output URI. If it
    raises, the case is recorded `status: failed` with the exception as
    `failure_mode`, and the loop continues with every remaining case.
    """
    results: List[Dict[str, Any]] = []
    for case in cases:
        start = clock()
        try:
            output_uri = generate(case)
            results.append({
                "case_id": case["case_id"],
                "task_type": case["task_type"],
                "latency_seconds": clock() - start,
                "seed": case["seed"],
                "output_uri": output_uri,
                "status": "ok",
                "failure_mode": None,
            })
        except Exception as exc:  # noqa: BLE001 — isolation is the point
            results.append({
                "case_id": case["case_id"],
                "task_type": case["task_type"],
                "latency_seconds": clock() - start,
                "seed": case["seed"],
                "output_uri": None,
                "status": "failed",
                "failure_mode": f"{type(exc).__name__}: {exc}",
            })
    return results


def build_run_record(
    run_id: str,
    model: str,
    instance_type: str,
    model_load_seconds: float,
    case_results: List[Dict[str, Any]],
    instance_hours: float,
    estimated_cost_usd: float,
    billing_reconciled_cost_usd: Optional[float] = None,
) -> Dict[str, Any]:
    """Assemble the metrics.json record (protocol §4)."""
    return {
        "run_id": run_id,
        "model": model,
        "instance_type": instance_type,
        "account": ACCOUNT,
        "region": REGION,
        "model_load_seconds": model_load_seconds,
        "cases": case_results,
        "instance_hours": instance_hours,
        "estimated_cost_usd": estimated_cost_usd,
        "billing_reconciled_cost_usd": billing_reconciled_cost_usd,
    }


def assert_run_record_complete(record: Dict[str, Any]) -> None:
    """Schema assertion enforced before a run is marked complete (Property 4).

    Raises MetricsSchemaError if any required run-level field is absent, any
    case is missing latency/seed/status, or any successful case is missing its
    output reference. billing_reconciled_cost_usd may be None (filled in
    Phase D reconciliation) but the key must exist.
    """
    for field in RUN_REQUIRED_FIELDS:
        if field not in record:
            raise MetricsSchemaError(f"run record missing field: {field}")

    for field in ("model_load_seconds", "instance_hours", "estimated_cost_usd"):
        if not isinstance(record[field], (int, float)) or isinstance(record[field], bool):
            raise MetricsSchemaError(f"run field {field} must be a number, got {record[field]!r}")

    cases = record["cases"]
    if not isinstance(cases, list) or not cases:
        raise MetricsSchemaError("run record must contain a non-empty cases list")

    for case in cases:
        for field in CASE_REQUIRED_FIELDS:
            if field not in case:
                raise MetricsSchemaError(
                    f"case {case.get('case_id', '?')} missing field: {field}")
        if case["task_type"] not in VALID_TASK_TYPES:
            raise MetricsSchemaError(
                f"case {case['case_id']}: invalid task_type {case['task_type']!r}")
        if case["status"] not in VALID_STATUSES:
            raise MetricsSchemaError(
                f"case {case['case_id']}: invalid status {case['status']!r}")
        if not isinstance(case["latency_seconds"], (int, float)) or isinstance(case["latency_seconds"], bool):
            raise MetricsSchemaError(
                f"case {case['case_id']}: latency_seconds must be a number")
        if not isinstance(case["seed"], int) or isinstance(case["seed"], bool):
            raise MetricsSchemaError(
                f"case {case['case_id']}: seed must be an integer")
        if case["status"] == "ok" and not case["output_uri"]:
            raise MetricsSchemaError(
                f"case {case['case_id']}: successful case missing output_uri")
        if case["status"] == "failed" and not case["failure_mode"]:
            raise MetricsSchemaError(
                f"case {case['case_id']}: failed case missing failure_mode")


def write_metrics(record: Dict[str, Any], out_path: Path) -> None:
    """Validate then write metrics.json — a run cannot be marked complete
    unless the schema assertion passes."""
    assert_run_record_complete(record)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
