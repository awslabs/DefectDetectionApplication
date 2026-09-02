# Bugfix Requirements Document

## Introduction

Every deployed workflow that combines a CAMERA source (Aravis frame grab) with
Custom Python nodes stalls for exactly 120 seconds and fails with
`Pipeline timed out after 120s without completing (no EOS/ERROR received)`.

Observed in production on jetson-thor1 (JP7, LocalServer 1.0.16) with workflow
`bdfabc2a-d246-466f-a4ca-53bb40c9e119` v5 (IMTS inspection: Basler camera →
`custom_python_preprocess_1` → tee/funnel → `n4` → `bedrock_inference_1`).
Executions `ed3b60aa-fa34-4eca-9b10-93a5284f5384`,
`1aed6b7f-7879-4b53-897b-8fd598098eff`, and
`f7e430b7-e13a-4ba5-af7c-c35fde6451f2` all failed identically.

The root cause is fully proven (see design.md for the evidence chain): in the
`WorkflowExecutor.execute` path of
`src/backend/workflow_engine/pipeline_executor.py`, the bridged-pipeline branch
passes the unmerged Custom-Python-source variable (`python_frame_data`) to the
bridged runner instead of the merged `frame_data`. For camera-fed workflows
`python_frame_data` is `None`, so the bridged pipeline never receives the
grabbed camera frame: no buffer and no EOS are ever pushed into the pipeline's
appsrc, and the pipeline waits until the 120 s watchdog fires.

The one-line fix has already been validated on-device via hot-patch (execution
`26f833f8-f687-4a59-89d2-1a8e2f79dcbc` completed in 3.4 s, re-verified after a
clean container restart). This spec drives the remaining repo-side work:
regression test, the fix in the repo, suite regression run, component build
(JP7 1.0.17), and on-device verification from a real built component.

Impact if unfixed: every camera + Custom-Python workflow on every device is
unusable — the primary inspection execution model (camera grab into custom
preprocessing into inference) always fails.

## Bug Analysis

### Current Behavior (Defect)

When a workflow document plans an Aravis frame feed (a successful camera grab,
no Custom Python source node) and also contains Custom Python node bridges:

1.1 WHEN the executor reaches the bridged pipeline branch THEN the system
invokes the bridged pipeline runner with `frame_data=python_frame_data`, which
is `None` for camera-fed workflows — the grabbed camera frame is dropped.

1.2 WHEN the bridged pipeline runs with `frame_data=None` THEN the system
never resolves a `fed_source`, never pushes a buffer or EOS into the pipeline's
appsrc, and the pipeline waits starved upstream (handlers idle on stdin,
appsrc thread in `g_cond_wait`).

1.3 WHEN such an execution runs THEN the system fails after exactly 120
seconds with `Pipeline timed out after 120s without completing (no EOS/ERROR
received)`.

### Expected Behavior (Correct)

2.1 WHEN the executor reaches the bridged pipeline branch for a document with
an Aravis frame feed THEN the system SHALL invoke the bridged pipeline runner
with the grabbed camera frame as `frame_data` (the merged `frame_data`
variable).

2.2 WHEN the bridged pipeline runs with the camera frame THEN the system SHALL
push the frame and EOS into the pipeline's appsrc, pump it through the Custom
Python node handlers, and the pipeline SHALL reach EOS.

2.3 WHEN such an execution runs THEN the system SHALL complete normally within
its usual runtime (seconds, not the 120 s watchdog), with downstream bindings
(e.g. `bedrock_inference`) processed.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a document has a Custom Python source node (a produced frame) and
Custom Python node bridges THEN the system SHALL CONTINUE TO invoke the
bridged runner with the produced frame as `frame_data` (after the merge at
`pipeline_executor.py` lines 1344–1345, `frame_data` and `python_frame_data`
are the same object for this family).

3.2 WHEN a document has Custom Python node bridges but no frame feed of either
kind (no Aravis feed, no Custom Python source) THEN the system SHALL CONTINUE
TO invoke the bridged runner without any `frame_data` keyword — the
pre-existing bridged call shape stays bit-identical.

3.3 WHEN a document has an Aravis frame feed but no bridges THEN the system
SHALL CONTINUE TO run through `run_pipeline(launch_string, frame_data)` — the
non-bridged branch was never affected.

3.4 WHEN a document has neither a frame feed nor bridges THEN the system SHALL
CONTINUE TO take the plain `run_pipeline(launch_string)` path unchanged.
