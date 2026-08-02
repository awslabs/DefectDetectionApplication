# OPC UA Output Node Bugfix Design

## Overview

This bugfix addresses two defects that together produce a "silent success" when the
`opcua_write` output binding (node `n4`) runs on the JP5 edge device:

1. **Missing runtime dependency.** `_default_opcua_writer`
   (`src/backend/workflow_engine/output_bindings.py`, ~line 373) does
   `from opcua import Client` and re-raises `ImportError` as a `RuntimeError`. The `opcua`
   (python-opcua) package is not listed in `src/backend/requirements.txt`, so it is never
   installed on the packaged edge component. Fix: add a pinned `opcua` entry to
   `requirements.txt` following the existing pinning convention.

2. **Run reported "completed" despite an output-binding failure.**
   `pipeline_executor.py` (~lines 1079–1091) sets `execution.status =
   EXECUTION_STATUS_COMPLETED`, commits, logs "completed", and only THEN calls
   `_run_post_run_handler`, which invokes `OutputBindingProcessor.process`. `process`
   isolates each binding failure in a `try/except` (intentional per Requirement 13.7) but
   never reports failures back. Fix: collect per-binding failures in the processor, and have
   the executor finalize the run as `failed` (identifying the failing node id[s]) when any
   output binding failed — while still attempting every binding.

The fix is minimal and targeted: it preserves binding isolation (13.7) and mirrors the
existing Bedrock/LLM inference failure-surfacing pattern (`_finish_failed` +
`_persist_node_status` with a `failing_node_id`).

## Glossary

- **Bug_Condition (C)**: (a) an `opcua_write` binding runs while `opcua` is absent from the
  packaged runtime, causing an import/RuntimeError; and/or (b) at least one output binding
  failed yet the run would be finalized as `completed`.
- **Property (P)**: (a) `opcua` is packaged and importable so the binding can connect/write/
  disconnect; (b) any run with a failed output binding ends in `EXECUTION_STATUS_FAILED` with
  the failing node id(s) recorded.
- **Preservation**: runs where every output binding succeeds (or there are none) finalize as
  `completed` exactly as before, and per-binding isolation (all bindings attempt to run) is
  unchanged.
- **`_default_opcua_writer`**: the default OPC UA writer in `output_bindings.py` that imports
  `opcua.Client`, connects, sets the node value, and disconnects.
- **`OutputBindingProcessor.process`**: the post-run handler (matching
  `PostRunHandler = (registration, compiled_document, tag_values) -> None`) that iterates
  `document["executorBindings"]` and runs each output binding inside a contained `try/except`.
- **`WorkflowExecutor.execute`**: the executor in `pipeline_executor.py` that finalizes the
  run status and calls `_run_post_run_handler`.
- **`EXECUTION_STATUS_COMPLETED` / `EXECUTION_STATUS_FAILED`**: terminal statuses defined in
  `pipeline_executor.py` (~lines 342–345), reported by the `/workflows/executions` status
  endpoint.
- **`failing_node_id`**: the `WorkflowExecution` field set by `_finish_failed` to identify the
  node that caused the failure.

## Bug Details

### Bug Condition

The bug manifests in two coupled ways. Part 1: on the edge device the `opcua_write` binding
cannot import its client because `opcua` was never packaged. Part 2: even when a binding fails
for any reason, the executor has already marked the run `completed` before the post-run handler
runs, and the handler swallows the failure — so the failure never reaches the terminal status.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type WorkflowRun
  OUTPUT: boolean

  RETURN (input.hasOpcuaWriteBinding = TRUE
          AND "opcua" NOT IN packagedRuntimeDependencies())
     OR  (atLeastOneOutputBindingFailed(input) = TRUE
          AND terminalStatus(input) = EXECUTION_STATUS_COMPLETED)
END FUNCTION
```

### Examples

- **Missing package (part 1):** A workflow with an `opcua_write` node `n4` runs on JP5.
  `_default_opcua_writer` executes `from opcua import Client` →
  `ModuleNotFoundError: No module named 'opcua'` → re-raised as
  `RuntimeError: The 'opcua' Python package is not available; it is delivered as a
  Workflow_Component dependency`. Expected: import succeeds, node value is written.
- **Silent success (part 2):** The same run's binding failure is caught by
  `process`'s `except Exception`, logged via `logger.exception(...)`, and the run is reported
  `completed`. Expected: run reported `failed` with `failing_node_id = "n4"`.
- **Isolation still required (preservation):** A run with an mqtt binding and an opcua binding
  where mqtt fails — the opcua binding must still be attempted (13.7). Expected: both attempted;
  because one failed, run ends `failed`.
- **All-success (preservation):** A run where every output binding succeeds must still finalize
  `completed` and log "Workflow execution %s completed".

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Per-binding isolation (Requirement 13.7): a failing output binding never prevents the
  remaining output bindings from being attempted. Failures are collected, not short-circuited.
- A run where all output bindings succeed (or has no output bindings) finalizes as `completed`
  and logs "Workflow execution %s completed" exactly as today.
- `_default_opcua_writer`'s connect → set_value → disconnect sequence is unchanged (the
  integration test injects a fake `opcua` module at the client boundary and asserts this).
- Existing Bedrock/LLM inference failure surfacing (`_finish_failed` + `_persist_node_status`
  with `failing_node_id`, returning early) is unchanged.
- Gating/conditional skipping of bindings continues to behave as before (a gated-out or
  condition-skipped binding is not a failure).

**Scope:**
All runs where `isBugCondition` is false must be completely unaffected. This includes:
- Runs with no output bindings.
- Runs where every output binding succeeds.
- Runs whose bindings are gated out or conditionally skipped.
- Bedrock / LLM inference failure paths (already surfaced upstream).

## Hypothesized Root Cause

1. **Dependency omission (part 1):** `src/backend/requirements.txt` never included `opcua`.
   The `_default_opcua_writer` comment asserts it is "delivered as a Workflow_Component
   dependency", but no packaged requirement or staging step actually installs it, so it is
   absent on JP5 (Python 3.11 / aarch64). The RuntimeError is the symptom, not the cause.

2. **Status finalized before post-run handler (part 2, primary):** In
   `WorkflowExecutor.execute` (~lines 1079–1091) the ordering is:
   `status = COMPLETED` → `commit()` → `_persist_node_status(success=True)` →
   log "completed" → `_run_post_run_handler(...)`. The handler outcome therefore cannot
   influence the terminal status.

3. **Failure swallowed in the processor (part 2, secondary):**
   `OutputBindingProcessor.process` catches every binding exception with
   `except Exception: logger.exception(...)` and returns `None`. There is no return value or
   collected error set for the executor to inspect. `_run_post_run_handler` also wraps the
   call in its own contained `try/except`, so even a raised error would currently be swallowed.

## Correctness Properties

Property 1: Bug Condition - OPC UA package packaged and binding failures fail the run

_For any_ input where the bug condition holds (isBugCondition returns true), the fixed system
SHALL (a) have `opcua` present in the packaged runtime dependencies so `import opcua` succeeds,
and (b) finalize a run that had at least one failed output binding as
`EXECUTION_STATUS_FAILED`, recording the failing output-binding node id(s) in the execution's
failure detail.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

Property 2: Preservation - Binding isolation and all-success behavior unchanged

_For any_ input where the bug condition does NOT hold (isBugCondition returns false), the fixed
system SHALL produce the same result as the original — runs with all bindings succeeding (or no
bindings) finalize `completed` and log "completed" — and, even when a binding fails, the fixed
system SHALL still attempt every output binding, preserving Requirement 13.7 isolation and the
existing Bedrock/LLM inference failure-surfacing path.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

## Fix Implementation

### Changes Required

Assuming the root-cause analysis is correct, two files change.

**File 1**: `src/backend/requirements.txt`

- Add a pinned `opcua` entry (python-opcua; import name `opcua`) following the file's existing
  convention: exact `==` pin, preserve `setuptools<81`. Confirm the chosen version publishes a
  wheel importable under JP5 Python 3.11 / aarch64 (python-opcua is a pure-Python package, so a
  `py3-none-any` wheel suffices; verify its transitive deps — e.g. `cryptography`,
  `python-dateutil`, `lxml`/`aiofiles` depending on version — resolve on the device, and add a
  short comment explaining why the pin was chosen, matching the style of the `grpcio-tools` /
  `dlr` comments already in the file).
- Do NOT change any other pins.

**File 2**: `src/backend/workflow_engine/output_bindings.py`

- **Function**: `OutputBindingProcessor.process`
- Collect failed bindings instead of only logging them. Keep the existing per-binding
  `try/except` (13.7) so every binding is still attempted, but on exception append the
  binding's `node_id` (and error string) to a `failed: List[Tuple[node_id, str]]` list, keep
  the existing `logger.exception(...)`, and continue the loop.
- After the loop, if `failed` is non-empty, raise a new dedicated exception that carries the
  failing node id(s), mirroring `BedrockInferenceError` (which carries `node_id`). Suggested:
  `class OutputBindingError(Exception)` with a `node_ids: List[str]` (or a primary
  `node_id`) attribute and a message summarizing the failed bindings.

**File 3**: `src/backend/workflow_engine/pipeline_executor.py`

- **Function**: `WorkflowExecutor.execute` (~lines 1079–1091) and `_run_post_run_handler`
  (~line 1452).
- Reorder finalization so the post-run handler runs BEFORE the terminal status is committed,
  OR run the handler first and let its outcome decide the status. Concretely:
  1. After the LLM inference block, invoke the output-binding handler and capture whether it
     reported failures (either by having `_run_post_run_handler` return the raised
     `OutputBindingError`/node ids, or by catching `OutputBindingError` around the handler
     call — mirroring the existing Bedrock `except ... as e: failing_node_id =
     getattr(e, "node_id", None)` block ~lines 1040–1063).
  2. If the handler reported one or more failed bindings: call
     `_finish_failed(session, execution, error=<summary>, failing_node_id=<first/only node
     id>)` and `_persist_node_status(session, execution, collector,
     failing_node_id=..., failure_detail=...)`, then return — do NOT log "completed".
  3. Otherwise (no failures): set `execution.status = EXECUTION_STATUS_COMPLETED`, commit,
     `_persist_node_status(success=True)`, and log "completed" exactly as today.
- Update `_run_post_run_handler` so an `OutputBindingError` propagates (or is returned to the
  caller) instead of being swallowed by its contained `try/except`; keep containment for any
  OTHER unexpected handler exception so an unrelated handler bug still can't crash the run
  outside the existing best-effort path.
- Reuse existing helpers `_finish_failed`, `_persist_node_status`, and the
  `_mark_failed_best_effort` fallback; do not introduce a new terminal status (use
  `EXECUTION_STATUS_FAILED`).

## Testing Strategy

### Validation Approach

Two-phase: first surface counterexamples on the UNFIXED code (a run whose `opcua_write` binding
fails is reported `completed`, and `opcua` is absent from `requirements.txt`), then verify the
fix packages `opcua` and finalizes such runs as `failed` while preserving binding isolation and
all-success behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix, and
confirm/refute the root-cause analysis.

**Test Plan**:
- Assert `opcua` is NOT currently listed in `src/backend/requirements.txt` (part 1
  counterexample — will pass on unfixed code as a demonstration, then flip to the fix assertion).
- Drive `WorkflowExecutor.execute` (or `OutputBindingProcessor.process` + finalization) with a
  compiled document containing an `opcua_write` binding whose injected `opcua_writer` raises
  (simulating the missing package / a write failure), plus at least one other binding, and
  assert the run's terminal status. On UNFIXED code the status is `completed` (the bug).

**Test Cases**:
1. **requirements.txt missing opcua** (part 1): parse `requirements.txt`; on unfixed code
   `opcua` is absent. (After fix: present and importable.)
2. **opcua binding failure reported completed** (part 2): a run with a failing `opcua_write`
   binding is finalized `completed` on unfixed code (will fail the assertion once we assert
   `failed`).
3. **Multi-binding failure still completed**: mqtt fails + opcua present; run reported
   `completed` on unfixed code.

**Expected Counterexamples**:
- `requirements.txt` has no `opcua` entry → `ModuleNotFoundError` on device.
- A run whose output binding raised is reported `completed` with no `failing_node_id`.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed system produces
the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  // part 1
  ASSERT "opcua" IN parse(requirements.txt) AND import_succeeds("opcua")
  // part 2
  execute'(input)
  ASSERT terminalStatus(input) = EXECUTION_STATUS_FAILED
  ASSERT failing_node_id(input) IN failedBindingNodeIds(input)
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed system
produces the same result as the original, and that binding isolation is preserved even on the
buggy path.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT execute(input) = execute'(input)   // all-success / no-bindings → completed
END FOR

FOR ALL input WHERE atLeastOneOutputBindingFailed(input) DO
  ASSERT everyOutputBindingAttempted'(input) = TRUE   // Requirement 13.7 isolation
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation because it
generates many binding-set configurations automatically and catches edge cases (ordering,
gating, mixed success/failure) that manual tests miss.

**Test Plan**: Observe UNFIXED behavior first — all-success runs report `completed`; a run with
mqtt failing still attempts the later opcua/dio bindings (the existing
`test_workflow_output_bindings.py` isolation assertion) — then write property-based tests
capturing that behavior and re-run after the fix.

**Test Cases**:
1. **All-success → completed**: every output binding succeeds → run `completed`, "completed"
   logged. Unchanged before/after.
2. **Isolation preserved**: with a failing binding among several, assert every other binding's
   runner was invoked (all attempted) — the invariant `test_workflow_output_bindings.py`
   already guards.
3. **Integration writer unchanged**: `_default_opcua_writer` with a fake `opcua` module still
   connects → set_value → disconnect (the `test_workflow_engine_integration.py` path).
4. **Bedrock/LLM failure surfacing unchanged**: a Bedrock inference failure still ends `failed`
   with its node id (no double-marking, no regression).

### Unit Tests

- `OutputBindingProcessor.process` collects failed node ids and raises `OutputBindingError`
  when any binding fails, while still invoking every binding's runner.
- `WorkflowExecutor.execute` finalizes `failed` with `failing_node_id` when the handler reports
  a failure, and `completed` when it does not.
- `requirements.txt` includes a pinned `opcua` entry.

### Property-Based Tests

- Generate random sets of output bindings with an arbitrary subset marked to fail: assert
  (a) every binding runner is invoked (isolation), and (b) the run ends `failed` iff at least
  one binding failed, else `completed`.
- Generate all-success binding sets: assert the fixed executor result equals the original
  (`completed`).

### Integration Tests

- End-to-end run with a fake `opcua` module injected at the client boundary (as in
  `test_workflow_engine_integration.py`): asserts connect/write/disconnect AND that the run is
  `completed` when the write succeeds.
- End-to-end run where the injected opcua writer raises: asserts the run ends `failed` with
  `failing_node_id` set to the opcua node, and that other bindings were still attempted.
- Deployment verification (manual): after rebuild/republish, confirm on-device that `opcua`
  imports and node `n4` writes successfully.
