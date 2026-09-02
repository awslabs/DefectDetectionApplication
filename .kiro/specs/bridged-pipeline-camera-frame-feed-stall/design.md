# Bridged Pipeline Camera Frame Feed Stall — Bugfix Design

## Overview

Camera-fed workflows containing Custom Python nodes stall for 120 s and fail
with `Pipeline timed out after 120s without completing (no EOS/ERROR
received)`. The root cause is a one-line variable mixup in
`src/backend/workflow_engine/pipeline_executor.py`: the bridged-pipeline
branch of `WorkflowExecutor.execute` forwards `python_frame_data` (the
Custom-Python-source produced frame, `None` for camera workflows) to
`_run_bridged` instead of the merged `frame_data` (which holds the Aravis
camera grab). The bridged runner therefore receives no frame; nothing is ever
pushed into the pipeline's appsrc and the watchdog fires at 120 s.

The fix is one line: `frame_data=python_frame_data` → `frame_data=frame_data`
in the bridged branch. It has already been validated on-device via hot-patch
on jetson-thor1 (see Evidence Chain). This spec's remaining work is the
regression test (must fail on unfixed code), the repo fix, a full
workflow_engine suite run, and a real component build (JP7 1.0.17) + deploy +
on-device end-to-end verification.

**Root cause status: PROVEN, not hypothesized.** Do not re-investigate the
pipeline topology, the handlers, or the bridge pump — see Red Herrings below.

## Glossary

- **Bug_Condition (C)**: the document plans an Aravis frame feed
  (`python_frame_data is None`, `frame_data is not None`) AND `bridge_specs`
  is non-empty (the document contains `emlpython` Custom Python nodes).
- **Property (P)**: the bridged pipeline runner receives the grabbed camera
  frame as `frame_data`, so the fed appsrc gets the buffer + EOS and the
  pipeline completes.
- **Preservation**: all runs where C does not hold keep today's exact call
  shape — python-source bridged runs still get the produced frame, feed-free
  bridged runs still get no `frame_data` keyword, non-bridged camera runs
  still go through `run_pipeline(launch_string, frame_data)`.
- **F**: `WorkflowExecutor.execute` as of `spec/jetpack7-support` HEAD
  `a2ad086` (unfixed — the buggy line is still present at
  `pipeline_executor.py:1600`).
- **F'**: the same path with the one-line fix applied.
- **frame_data**: the merged single-frame feed variable — the Aravis grab
  (assigned at line 1292), overwritten by the produced frame at lines
  1344–1345 when a Custom Python source exists.
- **python_frame_data**: the Custom Python source produced frame only;
  `None` for every camera-fed or source-free workflow.
- **bridge_specs**: `python_bridge.bridge_specs(document)` — one spec per
  `emlpython` element; non-empty routes execution to `_run_bridged` →
  `run_bridged_pipeline` instead of `GstPipelineManager.run_pipeline`.
- **fed_source**: the appsrc `run_bridged_pipeline` feeds when given
  `frame_data`; with `frame_data=None` it stays `None` and no buffer/EOS is
  ever pushed.

## Bug Details

### Bug Condition

The bug manifests in `WorkflowExecutor.execute`
(`src/backend/workflow_engine/pipeline_executor.py`, verified against the
current repo):

- line 1292: `frame_data = self._prepare_aravis_frame_feed(...)` — the camera
  grab.
- lines 1344–1345: `if python_frame_data is not None: frame_data =
  python_frame_data` — the merge for Custom-Python-source workflows.
- lines 1596–1600 (**the bug**): the bridged branch calls
  `self._run_bridged(..., frame_data=python_frame_data, ...)` — the UNMERGED
  python-source variable instead of `frame_data`.
- line 1608 (`elif frame_data is not None:`): the non-bridged branch correctly
  uses `frame_data` — only the bridged branch is wrong.

Inside `_run_bridged` (line 2576), `frame_data=None` means the `frame_data`
kwarg is omitted entirely from the runner invocation, so
`run_bridged_pipeline` runs feed-free: `fed_source` stays `None`, no buffer
and no EOS are pushed, and the pipeline waits forever until the 120 s
watchdog.

**Formal Specification:**

```
FUNCTION isBugCondition(execution)
  INPUT: execution — a workflow run reaching the pipeline-run branch of
         WorkflowExecutor.execute
  OUTPUT: boolean

  RETURN execution.document plans an Aravis frame feed
           (frame_data IS NOT None from _prepare_aravis_frame_feed)
         AND execution.document has no Custom Python source
           (python_frame_data IS None)
         AND python_bridge.bridge_specs(execution.document) is non-empty
END FUNCTION
```

Under the bug, for all executions where `isBugCondition` holds:
`run_bridged_pipeline` receives `frame_data=None` (kwarg absent). Expected:
it receives the grabbed camera frame.

### Examples

- Workflow `bdfabc2a-d246-466f-a4ca-53bb40c9e119` v5 on jetson-thor1 (Basler
  camera → `custom_python_preprocess_1` → tee/funnel → `n4` →
  `bedrock_inference_1`): executions `ed3b60aa`, `1aed6b7f`, `f7e430b7` all
  stalled 120 s and failed with "no EOS/ERROR received". Expected: completes
  in seconds with a Bedrock verdict.
- The same workflow with the one-line fix hot-patched: execution
  `26f833f8-f687-4a59-89d2-1a8e2f79dcbc` completed in 3.4 s with
  `is_anomalous: true, confidence: 0.988`.
- A Custom-Python-source workflow with bridges (non-bug input): works today
  and must keep working — after the merge, `frame_data ==
  python_frame_data`, so the fixed call passes the identical object.
- A bridged workflow with no feed of either kind (non-bug input): both
  variables are `None`; fixed and unfixed calls are bit-identical.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**

- Custom-Python-source bridged runs: the runner receives the produced frame
  (Requirement 3.1). The merge at lines 1344–1345 guarantees
  `frame_data is python_frame_data` for this family, so
  `frame_data=frame_data` is behavior-identical.
- Feed-free bridged runs: the runner is invoked without any `frame_data`
  keyword — `_run_bridged` only adds the kwarg when the value is not `None`
  (Requirement 3.2).
- Non-bridged Aravis runs: `run_pipeline(launch_string, frame_data)` — this
  branch (line 1608) already used the merged variable and is untouched
  (Requirement 3.3).
- Feed-free, bridge-free runs: plain `run_pipeline(launch_string)`
  (Requirement 3.4).

**Scope:**

The fix changes exactly one keyword argument in one call site. All inputs
where `isBugCondition` is false are unaffected by construction: for
python-source runs the two variables are the same object; for feed-free runs
both are `None`.

## Hypothesized Root Cause

**The root cause is PROVEN, not hypothesized** — recorded here with the full
evidence chain so future diagnosticians do not repeat the investigation.

### Evidence Chain

1. **Prior fixes ruled out.** Prior fix commit `325772e` (preroll pump on
   new-preroll, `BRIDGE_APPSINK_CAPS` RGB, dangling appsrc→fakesink, I420
   capsfilter before jpegenc — see
   `test/backend-test/workflow_engine/test_python_bridge_pipeline_stall.py`)
   was confirmed fully deployed (image digest `sha256:3b7a514eb216d3418`, JP7
   LocalServer 1.0.16) and the workflow document repackaged (v5 carries the
   I420 capsfilter) — the stall was unchanged. Those fixes are real and still
   needed; this defect sits a layer above them (the frame never enters the
   pipeline at all).
2. **Topology and handlers ruled out.** Host-shaped harness
   `.kiro/harness/defectE_funnel_diag.py`, run INSIDE the thor1 backend
   container with the verbatim v5 launch string, the real v5 handler
   artifacts, a 4608x3288 synthetic bayer frame, and `frame_data` passed
   explicitly: completed in 1.1 s with both jpegs written. The tee/funnel/
   two-bridge topology and the handlers are NOT the bug.
3. **py-spy during a live stall:** executor thread idle in `loop.run()`
   (`python_bridge.py:1955`); both handler subprocesses (PIDs 1501/1538) idle
   in `_read_exact` waiting on stdin — no frame was ever dispatched.
4. **gdb `thread apply all bt` during a live stall:** the `appsrc:src` thread
   in `g_cond_wait` inside libgstapp (waiting for data), all queue threads
   empty-waiting, both `py_out_*` appsrc threads waiting — everything starved
   upstream of the fed appsrc.
5. **fed_source proven None.** An instrumented hot-patch logging the
   `set_state` return and the push-buffer/EOS flow returns inside
   `if fed_source is not None:` — the DIAG line NEVER appeared during a stall
   (while the executor was at `loop.run()` past that point) → `fed_source`
   was `None` → `frame_data` arrived `None` at `run_bridged_pipeline`.
6. **Fix validated on-device.** The one-line fix hot-patched into the running
   container: execution `26f833f8-f687-4a59-89d2-1a8e2f79dcbc` completed in
   3.4 s, `Bedrock inference binding (node bedrock_inference_1) processed`,
   tags `is_anomalous: true, confidence: 0.988`. Re-verified after a clean
   container restart with only the fix applied: completed in 4 s.
7. **Custom handler code ruled out.** The Bedrock-generated handlers —
   preprocess (process_frame contract, reference image emitter from
   `/aws_dda/imts/reference`) and `n4` (handle contract, JSON extraction from
   llm metadata) — both ran correctly in the harness.

### Red Herrings (for future diagnosticians)

- The funnel/tee topology was innocent (proven by the harness run, item 2).
- "No bridge pump logs" is NOT evidence of a pipeline stall — a healthy 1.1 s
  harness run logs nothing from the pump either.
- Queue `max-size-bytes` vs 45 MB RGB frames was a plausible but wrong theory.

## Correctness Properties

Property 1: Bug Condition - Bridged runner receives the camera frame

_For any_ execution where the bug condition holds (the document plans an
Aravis frame feed, has no Custom Python source, and has non-empty
bridge_specs), the fixed executor SHALL invoke the bridged pipeline runner
with the grabbed camera frame as `frame_data`, so the run completes instead of
stalling to the 120 s watchdog.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation - Non-camera bridged and non-bridged call shapes

_For any_ execution where the bug condition does NOT hold (Custom-Python-
source bridged runs, feed-free bridged runs, non-bridged camera runs,
feed-free bridge-free runs), the fixed executor SHALL invoke the pipeline
runner/manager with exactly the same arguments as the original executor,
preserving the produced-frame forwarding, the feed-free bridged call shape
(no `frame_data` keyword), and the non-bridged `run_pipeline` paths.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

## Fix Implementation

### Changes Required

**File**: `src/backend/workflow_engine/pipeline_executor.py`

**Function**: `WorkflowExecutor.execute`, bridged branch (lines 1596–1600)

**Specific Change** (one line, validated on-device):

```python
# before (line 1600):
    frame_data=python_frame_data,
# after:
    frame_data=frame_data,
```

When a Custom Python source exists, `frame_data` has already been overwritten
with `python_frame_data` at lines 1344–1345, so the merged variable is correct
for both source families. The surrounding comment (lines 1586–1595) should be
updated to reflect that the merged Frame_Feed (Aravis grab OR produced frame)
is forwarded.

No other files change (besides the new regression tests).

### Repo State

- Current checkout: `integration/all-specs` (merge of `spec/jetpack7-support`
  HEAD `a2ad086`); the fix is NOT in the repo (verified: line 1600 still reads
  `frame_data=python_frame_data`).
- thor1's backend container is running with the fix hot-patched — it will
  revert whenever the container is recreated (e.g. any new deployment). The
  repo fix + build is what makes it durable.

## Testing Strategy

### Validation Approach

Two-phase: first write an executor-level exploration test that FAILS on
unfixed code (proving the runner receives no frame), and preservation tests
that PASS on unfixed code (baselining the non-bug call shapes); then apply the
fix and confirm the exploration test passes and the preservation tests still
pass. Finally, because this is on-device edge behavior, verify from a real
built component on jetson-thor1 per the repo's mandatory on-device rule.

### Exploratory Bug Condition Checking

**Goal**: Demonstrate on UNFIXED code that the bridged runner is invoked
without the camera frame. (The root cause is already proven on-device; this
test pins it in the repo so it can never regress silently.)

**Test Plan**: Follow the established executor test style in
`test/backend-test/workflow_engine/` — no GStreamer needed:

- Build a compiled document that combines the Aravis shape from
  `test_workflow_aravis_executor.py` (`aravisBinding: true` binding point +
  appsrc element) with an `emlpython` element as in
  `test_workflow_python_bridge.py` (handler file written into the artifact
  set's `python/<node>/handler.py`).
- Inject a `FakeCameraManager`-style `frame_grabber` returning a known frame
  object, and a recording `bridged_pipeline_runner` double that captures
  `(launch_string, bridges, kwargs)`.
- Use `pipeline_manager_factory=ExplodingPipelineManager` so any fallthrough
  to the plain manager fails loudly.
- Execute and assert the runner was called once with
  `kwargs["frame_data"] is grabber.frame`.

**Expected Counterexample on unfixed code**: the runner is invoked with NO
`frame_data` keyword at all (`_run_bridged` omits the kwarg when the value is
`None`) — the assertion fails, matching the on-device diagnosis
(`fed_source is None`).

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed
executor forwards the camera frame.

**Pseudocode:**

```
FOR ALL execution WHERE isBugCondition(execution) DO
  run F'(execution) with injected frame_grabber and bridged runner double
  ASSERT runner received frame_data IS the grabbed frame
  ASSERT execution status = COMPLETED
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold,
the fixed executor produces the same runner/manager invocations as the
original.

**Pseudocode:**

```
FOR ALL execution WHERE NOT isBugCondition(execution) DO
  ASSERT F(execution) call shape = F'(execution) call shape
END FOR
```

**Testing Approach**: Observation-first — run each non-bug family on UNFIXED
code, record the exact runner/manager invocation, and encode it as the
assertion. Much of this is already pinned by the existing suites
(`test_workflow_python_source_executor.py`,
`test_workflow_python_bridge.py`, `test_workflow_aravis_executor.py`); the
preservation tests add explicit coverage for the call-shape identities the
fix relies on.

**Test Cases**:

1. **Python-source bridged run**: document with a Custom Python source and an
   `emlpython` bridge — the runner receives the produced frame
   (`frame_data is` the produced frame), identical before/after the fix.
2. **Feed-free bridged run**: `emlpython` bridge, no Aravis point, no source
   — the runner is invoked with no `frame_data` keyword, identical
   before/after the fix.
3. **Non-bridged Aravis run**: Aravis point, no bridges —
   `run_pipeline(launch_string, frame_data)` with the grabbed frame,
   identical before/after the fix (already covered by
   `test_workflow_aravis_executor.py`; keep it green).

### Unit Tests

- Exploration test: camera + bridges → runner gets the grabbed frame (fails
  on unfixed code).
- Preservation tests: the three non-bug families above keep their exact call
  shapes.

### Property-Based Tests

- The bug condition is deterministic (a fixed wrong variable on a fixed
  branch), so the exploration property is scoped to the concrete
  camera+bridges document family rather than randomized. Existing
  property suites (`test_property_aravis_free_execution_identity.py`,
  `test_property_python_source_free_identity.py`) already cover the
  feed-absence identities and must stay green.

### Integration Tests

- Full workflow_engine backend suite
  (`test/backend-test/workflow_engine/`) for regressions.
- On-device end-to-end (MANDATORY per repo rules, from a real built
  component, not a hot-patch): build JP7 LocalServer 1.0.17, deploy to
  jetson-thor1, run workflow `bdfabc2a-d246-466f-a4ca-53bb40c9e119` v5, and
  confirm completion in seconds with a Bedrock verdict and a healthy backend
  (no crash/restart) for a sustained period.
