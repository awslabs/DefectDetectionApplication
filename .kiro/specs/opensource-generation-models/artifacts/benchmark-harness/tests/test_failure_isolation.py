"""Failure-isolation test for the benchmark case loop.

**Property 5: Failure isolation in benchmark runs** — a failing case is
recorded with its failure mode and every remaining case still executes.
**Validates: Requirements 2.10**
"""

from runner import run_cases

CASES = [
    {"case_id": "inpaint-001", "task_type": "inpainting", "prompt": "p1", "seed": 101},
    {"case_id": "inpaint-002", "task_type": "inpainting", "prompt": "p2", "seed": 102},
    {"case_id": "inpaint-003", "task_type": "inpainting", "prompt": "p3", "seed": 103},
    {"case_id": "t2i-001", "task_type": "text_to_image", "prompt": "p4", "seed": 201},
    {"case_id": "t2i-002", "task_type": "text_to_image", "prompt": "p5", "seed": 202},
]


def test_mid_run_failure_is_recorded_and_remaining_cases_execute():
    executed = []

    def generate(case):
        executed.append(case["case_id"])
        if case["case_id"] == "inpaint-002":  # inject a failure mid-run
            raise RuntimeError("CUDA out of memory")
        return f"s3://bucket/{case['case_id']}.png"

    results = run_cases(CASES, generate)

    # Every case was attempted, in manifest order, despite the mid-run failure
    assert executed == [c["case_id"] for c in CASES]
    assert len(results) == len(CASES)

    # The failing case recorded status failed + failure_mode
    failed = next(r for r in results if r["case_id"] == "inpaint-002")
    assert failed["status"] == "failed"
    assert "CUDA out of memory" in failed["failure_mode"]
    assert failed["output_uri"] is None

    # All remaining cases (before and after the failure) succeeded with outputs
    for r in results:
        if r["case_id"] == "inpaint-002":
            continue
        assert r["status"] == "ok"
        assert r["failure_mode"] is None
        assert r["output_uri"] == f"s3://bucket/{r['case_id']}.png"
