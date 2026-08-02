# Bugfix Requirements Document

## Introduction

On the JP5 edge device, the `opcua_write` output binding (workflow node `n4`) fails at run time
with `ModuleNotFoundError: No module named 'opcua'`, which `_default_opcua_writer` in
`src/backend/workflow_engine/output_bindings.py` re-raises as
`RuntimeError: The 'opcua' Python package is not available; it is delivered as a
Workflow_Component dependency`. The `opcua` (python-opcua) package is not present on the device
because it was never added to the packaged runtime dependency list
(`src/backend/requirements.txt`), despite the code comment claiming it is delivered as a
Workflow_Component dependency.

Compounding this, the run's execution log reports the workflow "completed" successfully even
though the OPC UA output binding failed. The pipeline executor
(`src/backend/workflow_engine/pipeline_executor.py`) sets the execution status to
`completed`, commits, and logs "completed" BEFORE it invokes the post-run output-binding
handler. `OutputBindingProcessor.process` isolates each binding failure inside a
`try/except` (intentional, per Requirement 13.7 — one binding's failure must not stop the
others) but never surfaces that a binding failed back to the run's terminal status. The result
is a silent success: a failed output binding is reported as a completed run.

This is a two-part bugfix:
1. Add the missing `opcua` runtime dependency so the binding can execute on the edge device.
2. Make the run's terminal status reflect output-binding failures, while preserving the
   per-binding isolation of Requirement 13.7 (all bindings still attempt to run).

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the `opcua_write` output binding (node `n4`) executes on the JP5 edge device THEN the system raises `ModuleNotFoundError: No module named 'opcua'` inside `_default_opcua_writer`, which is re-raised as `RuntimeError: The 'opcua' Python package is not available; it is delivered as a Workflow_Component dependency`

1.2 WHEN the packaged runtime dependency list `src/backend/requirements.txt` is installed on the edge device THEN the system does NOT install the `opcua` (python-opcua) package because it is absent from that list, so the `from opcua import Client` import in `_default_opcua_writer` fails

1.3 WHEN one or more output bindings fail during `OutputBindingProcessor.process` (e.g. the `opcua_write` binding raising the RuntimeError above) THEN the system logs each failure via `logger.exception(...)` and continues, but the run's terminal status is still `completed` and the execution log reports "Workflow execution %s completed"

1.4 WHEN the pipeline executor finalizes a run THEN the system sets `execution.status = EXECUTION_STATUS_COMPLETED`, commits, and logs "completed" BEFORE calling `_run_post_run_handler`, so the post-run handler's outcome cannot influence the terminal status

### Expected Behavior (Correct)

2.1 WHEN the `opcua_write` output binding executes on the JP5 edge device THEN the system SHALL have the `opcua` (python-opcua) package installed so `from opcua import Client` succeeds and the binding can connect, write, and disconnect without raising `ModuleNotFoundError`/`RuntimeError` for a missing package

2.2 WHEN the packaged runtime dependency list `src/backend/requirements.txt` is installed on the edge device THEN the system SHALL install the `opcua` package pinned to a version that provides an aarch64 / cp311 wheel compatible with JP5 (Python 3.11), following the file's existing pinning convention (exact `==` pins, `setuptools<81` cap preserved)

2.3 WHEN one or more output bindings fail during `OutputBindingProcessor.process` THEN the system SHALL mark the run's terminal status as `failed` (not `completed`) and record the failing output-binding node id(s) in the execution's failure detail, consistent with how Bedrock inference failures are already surfaced

2.4 WHEN the pipeline executor finalizes a run that had at least one output-binding failure THEN the system SHALL NOT log/report the run as "completed"; the terminal status and execution log SHALL reflect the failure

### Unchanged Behavior (Regression Prevention)

3.1 WHEN multiple output bindings are present and one binding fails (e.g. an mqtt binding) THEN the system SHALL CONTINUE TO attempt every other output binding (opcua, digital output, etc.) — per-binding isolation from Requirement 13.7 is preserved; failures are collected, not short-circuited

3.2 WHEN all output bindings succeed THEN the system SHALL CONTINUE TO mark the run `completed` and log "Workflow execution %s completed" exactly as before

3.3 WHEN the `opcua` package is present and the `opcua_write` binding runs end-to-end THEN the system SHALL CONTINUE TO connect to the endpoint, set the node value, and disconnect via `_default_opcua_writer` (the real writer path used by the integration test with a fake `opcua` module injected at the client boundary)

3.4 WHEN a run has no output bindings, or its bindings are gated out / conditionally skipped THEN the system SHALL CONTINUE TO finalize as `completed` with no behavioral change

3.5 WHEN a Bedrock inference or LLM inference binding fails THEN the system SHALL CONTINUE TO mark the run `failed` with the failing node id, exactly as it does today (this bugfix must not alter that existing surfacing path)

## Bug Condition and Property Specification

### Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type WorkflowRun
  OUTPUT: boolean

  // Part 1: the opcua package is missing from the packaged runtime,
  //         so the opcua_write binding cannot import its client.
  // Part 2: at least one output binding failed, yet the run would be
  //         finalized as "completed".
  RETURN (X.hasOpcuaWriteBinding = TRUE
          AND "opcua" NOT IN packagedRuntimeDependencies())
     OR  (atLeastOneOutputBindingFailed(X) = TRUE
          AND terminalStatus(X) = EXECUTION_STATUS_COMPLETED)
END FUNCTION
```

### Property: Fix Checking

```pascal
// Part 1: with the fix, opcua is packaged and importable.
FOR ALL X WHERE X.hasOpcuaWriteBinding DO
  ASSERT "opcua" IN packagedRuntimeDependencies()
  ASSERT import_succeeds("opcua")
END FOR

// Part 2: with the fix, a run with any failed output binding ends failed,
//         identifying the failing node id(s).
FOR ALL X WHERE atLeastOneOutputBindingFailed(X) DO
  execute'(X)
  ASSERT terminalStatus(X) = EXECUTION_STATUS_FAILED
  ASSERT failingNodeIds(X) ⊇ failedBindingNodeIds(X)
END FOR
```

### Property: Preservation Checking

```pascal
// For all non-buggy inputs — every output binding succeeds, or there are no
// output bindings — the fixed executor behaves identically to the original,
// AND per-binding isolation (all bindings attempt to run) is unchanged.
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT execute(X) = execute'(X)
END FOR

// Isolation is preserved even in the buggy path: a failing binding never
// prevents the remaining bindings from being attempted.
FOR ALL X WHERE atLeastOneOutputBindingFailed(X) DO
  ASSERT everyOutputBindingAttempted'(X) = TRUE
END FOR
```

**Key Definitions:**
- **F**: the executor + `OutputBindingProcessor.process` as they exist before the fix
- **F'**: the executor + processor after adding `opcua` to `requirements.txt` and surfacing
  output-binding failures into the run's terminal status
- **Counterexample**: a run whose `opcua_write` binding raises the missing-package
  `RuntimeError` yet the run is reported `completed`
