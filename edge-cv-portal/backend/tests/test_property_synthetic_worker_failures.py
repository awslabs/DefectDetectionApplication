"""Property test for generation worker failure isolation (synthetic-
defect-data-generation, task 4.8).

**Feature: synthetic-defect-data-generation, Property 6: Partial failure
isolation in the generation worker**

_For any_ generation plan and any subset of tasks whose model invocation
fails: running the worker loop produces a completed Preview_Image
(retaining the task's resolved prompt text) for every non-failing task
and a recorded per-Variation failure (with the failure reason) for every
failing task, and the completed and failed sets exactly partition the
plan.

**Validates: Requirements 4.5, 1.4, 5.6**

Drives synthetic_data.execute_generation_tasks (the worker's task loop)
with a stubbed Bedrock invocation whose failing subset is injected by
Hypothesis. No AWS mocks needed: the loop's I/O seam is the injected
``invoke_task`` callable.
"""
import os
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "functions"))


@pytest.fixture(scope="module")
def worker_seams():
    """(build_generation_plan, execute_generation_tasks) imported lazily.

    Some standalone tests install a fake ``shared_utils`` into
    sys.modules at collection time; importing synthetic_data at run time
    (popping any fake first) binds the real layer module, the same way
    conftest's aws_stack does."""
    shared = sys.modules.get("shared_utils")
    if shared is not None and not hasattr(shared, "check_user_access"):
        sys.modules.pop("shared_utils", None)
        sys.modules.pop("synthetic_data", None)
    from synthetic_core import build_generation_plan
    from synthetic_data import execute_generation_tasks
    return build_generation_plan, execute_generation_tasks

prompts = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=0x2FF),
    min_size=1, max_size=60,
)
source_keys = st.from_regex(r"datasets/[a-z0-9]{1,10}\.png", fullmatch=True)


@st.composite
def plan_inputs(draw):
    """(sources, variation_count, resolved_prompt, base_seed,
    failing_fractions) - raw plan inputs plus an arbitrary failing subset
    encoded as per-task booleans (possibly empty, possibly everything)."""
    sources = draw(st.lists(source_keys, min_size=1, max_size=4,
                            unique=True))
    variation_count = draw(st.integers(min_value=1, max_value=5))
    resolved_prompt = draw(prompts)
    base_seed = draw(st.integers(min_value=0, max_value=858_993_459))
    task_count = len(sources) * variation_count
    fail_flags = draw(st.lists(st.booleans(), min_size=task_count,
                               max_size=task_count))
    return sources, variation_count, resolved_prompt, base_seed, fail_flags


@settings(deadline=None)
@given(case=plan_inputs())
def test_worker_partial_failure_isolation(worker_seams, case):
    """Completed and failed sets exactly partition the plan; completed
    previews retain the resolved prompt; failures carry reasons
    (Requirements 4.5, 1.4, 5.6)."""
    build_generation_plan, execute_generation_tasks = worker_seams
    sources, variation_count, resolved_prompt, base_seed, fail_flags = case
    plan = build_generation_plan(
        {"generation_model_id": "amazon.nova-canvas-v1:0"},
        sources, variation_count, resolved_prompt, {"seed": base_seed})
    failing = {index for index, flag in enumerate(fail_flags) if flag}
    observed = []

    def invoke_task(task):
        if task["task_index"] in failing:
            raise RuntimeError(f"injected-bedrock-failure-"
                               f"{task['task_index']}")
        return {"staging_key": f"staged/{task['task_index']}.png",
                "generation_method": "image_variation"}

    completed, failed = execute_generation_tasks(
        plan, invoke_task, on_result=observed.append)

    # Exact partition of the plan (Req 4.5): every task produced exactly
    # one preview, completed for non-failing tasks, failed for failing.
    assert len(completed) + len(failed) == len(plan)
    assert len(observed) == len(plan)
    completed_keys = {(p["source_image_key"], p["variation_index"])
                      for p in completed}
    failed_keys = {(p["source_image_key"], p["variation_index"])
                   for p in failed}
    assert completed_keys.isdisjoint(failed_keys)

    expected_failed_keys = {
        (task["source_image"], task["variation_index"])
        for task in plan if task["task_index"] in failing}
    expected_completed_keys = {
        (task["source_image"], task["variation_index"])
        for task in plan if task["task_index"] not in failing}
    assert failed_keys == expected_failed_keys
    assert completed_keys == expected_completed_keys

    # Completed previews retain the task's resolved prompt text (Req 5.6)
    # and are marked completed with the invocation's extra fields.
    for preview in completed:
        assert preview["status"] == "completed"
        assert preview["resolved_prompt"] == plan[0]["resolved_prompt"]
        assert preview["staging_key"].startswith("staged/")
        assert preview["approval_state"] == "pending"

    # Every failing task records a per-Variation failure with its reason
    # (Req 4.5, 1.4).
    for preview in failed:
        assert preview["status"] == "failed"
        assert preview["failure_reason"].startswith(
            "injected-bedrock-failure-")
        assert preview["resolved_prompt"] == plan[0]["resolved_prompt"]

    # The loop never stops early: on_result saw every task in order.
    assert [p["status"] for p in observed] == [
        "failed" if task["task_index"] in failing else "completed"
        for task in plan]
