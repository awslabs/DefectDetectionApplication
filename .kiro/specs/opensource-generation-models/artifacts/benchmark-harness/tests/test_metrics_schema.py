"""Metrics schema assertion tests.

**Property 4: Benchmark result-record completeness** — a complete run record
passes; records missing latency, seed, status, output reference,
instance-hours, or estimated cost are rejected before a run can be marked
complete.
**Validates: Requirements 2.6, 2.7**
"""

import pytest

from runner import MetricsSchemaError, assert_run_record_complete, build_run_record


def _complete_record():
    cases = [
        {"case_id": "inpaint-001", "task_type": "inpainting",
         "latency_seconds": 12.4, "seed": 101,
         "output_uri": "s3://bucket/inpaint-001.png",
         "status": "ok", "failure_mode": None},
        {"case_id": "t2i-001", "task_type": "text_to_image",
         "latency_seconds": 8.1, "seed": 201,
         "output_uri": None,
         "status": "failed", "failure_mode": "RuntimeError: OOM"},
    ]
    return build_run_record(
        run_id="flux1dev-r1", model="flux.1-dev", instance_type="g6e.xlarge",
        model_load_seconds=95.0, case_results=cases,
        instance_hours=1.5, estimated_cost_usd=2.79,
    )


def test_complete_record_passes():
    assert_run_record_complete(_complete_record())  # must not raise


def test_missing_case_latency_rejected():
    record = _complete_record()
    del record["cases"][0]["latency_seconds"]
    with pytest.raises(MetricsSchemaError, match="latency_seconds"):
        assert_run_record_complete(record)


def test_missing_case_seed_rejected():
    record = _complete_record()
    del record["cases"][0]["seed"]
    with pytest.raises(MetricsSchemaError, match="seed"):
        assert_run_record_complete(record)


def test_missing_case_status_rejected():
    record = _complete_record()
    del record["cases"][0]["status"]
    with pytest.raises(MetricsSchemaError, match="status"):
        assert_run_record_complete(record)


def test_successful_case_without_output_reference_rejected():
    record = _complete_record()
    record["cases"][0]["output_uri"] = None
    with pytest.raises(MetricsSchemaError, match="output_uri"):
        assert_run_record_complete(record)


def test_missing_instance_hours_rejected():
    record = _complete_record()
    del record["instance_hours"]
    with pytest.raises(MetricsSchemaError, match="instance_hours"):
        assert_run_record_complete(record)


def test_missing_estimated_cost_rejected():
    record = _complete_record()
    del record["estimated_cost_usd"]
    with pytest.raises(MetricsSchemaError, match="estimated_cost_usd"):
        assert_run_record_complete(record)


def test_non_numeric_estimated_cost_rejected():
    record = _complete_record()
    record["estimated_cost_usd"] = None
    with pytest.raises(MetricsSchemaError, match="estimated_cost_usd"):
        assert_run_record_complete(record)


def test_failed_case_without_failure_mode_rejected():
    record = _complete_record()
    record["cases"][1]["failure_mode"] = None
    with pytest.raises(MetricsSchemaError, match="failure_mode"):
        assert_run_record_complete(record)
