# Cold Model First Run Failure — Task 6 Hardware Verification Notes

Spec: `.kiro/specs/cold-model-first-run-failure` (bugfix).
Device: `jetson-thor1` (JetPack 7, `arm64_jp7`), account 164152369890, us-east-1.
Date: 2026-09-23, all times UTC.

Build and deployment under test are shared with
`.kiro/specs/static-camera-workflow-binding-invisible/verification-notes.md`:
`aws.edgeml.dda.LocalServer.arm64JP7` **1.0.44** built from
`wip/device-bugfixes-jp7-verify` by build job
`0fb6e82a-6341-45b1-b0b0-eb2575175e6f`, deployed as
`fbd816b5-07a6-46a4-85e2-d84c5843d71b`. `/dda_triton/model_readiness.py` and the
two call sites were confirmed present in the running container first.

## (a) Engine path — the deterministic reproduction, three times

Procedure each cycle: `docker restart` the backend (Triton load state is
per-process, so every restart re-opens the cold window), then trigger the
Triton-backed deployed workflow `ae783ac8-3cf2-4baf-b242-a3bb284776a9` v2
immediately.

| Cycle | Restart | First trigger | Gate log | Outcome |
|---|---|---|---|---|
| 1 | 03:45:00Z | 03:45:02.944Z | `model-blue-plate-rfdetr-small-jetson-xavier-jp7 is UNKNOWN; requesting a load before waiting` → `reached READY after 3.0s of waiting` (03:45:05.947Z) | execution `2c89efac` **completed** |
| 2 | 03:47:03Z | 03:47:10.877Z | same pair, `reached READY after 3.0s` (03:47:13.887Z) | execution `285dac32` **completed** |
| 3 | 04:16:48Z | 04:18:01.722Z | same pair, `reached READY after 3.0s` (04:18:04.723Z) | execution `454aab60` **completed** |

In every cycle `Setting pipeline to PLAYING state` follows the gate's success,
and the string `Pipeline failed to change state to PLAYING` does **not** appear
anywhere in the post-fix log.

**Run tally.** 11 engine executions between 03:43:41Z and 04:29:20Z, all
`completed`, **0 failed** — three of them the first trigger after a restart, the
exact case that failed before. Pre-fix baseline on this device: 1 of 12
executions failed on 2026-09-22 (`failed` at 18:20:26Z, error
`Pipeline failed to change state to PLAYING, check logs above this.`), and the
row is still in `workflow_executions` as the before-picture. The same error text
accounts for 6 historical failures in that table, first seen 2026-08-26.

Every completed run produced identical output (3 × `blue_plate`,
0.9506 / 0.9403 / 0.9395), so the gate adds a wait and changes nothing else.

## (b) Classic path — `POST /workflows/{id}/run`, folder image source

Backend restarted 04:16:48Z; the **first** use of a different, therefore cold,
Triton model was the run below at 04:16:56Z — 8 s after restart.

- Workflow `pagb7vj8` (`yolotest`), image source `pgc367hy` = Folder
  `/aws_dda/yolotest`, model `model-yolo-test-jetson-xavier-jp7`.
- Gate: `Triton model model-yolo-test-jetson-xavier-jp7 is UNKNOWN; requesting a
  load before waiting` (04:16:56.140Z) → `reached READY after 3.0s of waiting`
  (04:16:59.144Z) → `Setting pipeline to PLAYING state` (04:16:59.322Z).
- Response: **http 200**, `captureId
  pagb7vj8-478fba9115034048a63c863af2308766`, `processingTime` 3368 ms (the 3.0 s
  gate wait plus ~0.37 s of pipeline), full result document returned.
- **Input consumption (Requirement 2.2, design Decision 7).**
  `/aws_dda/inference-results/pagb7vj8/failed/` was **not** written to: its mtime
  is still `Sep 8 20:03` and its only contents are two pre-existing leftovers from
  2026-08-15 (`dog.jpg`, `horses.jpg`). Nothing from this run was moved there.
  The processed input (`restock-bus.jpg`) was removed from the folder by the
  normal success path
  (`gst_pipeline_executor._cleanup_file_after_processing`, "on successful
  inference pipeline run, remove the source image file for folder-based image
  sources to make way for the next execution") — that is the documented
  success-path behaviour, not the `failed/` move the bug produced.

So on the classic path the cold model now costs a logged wait instead of a
generic error plus a relocated input.

## (c) Warm behaviour unchanged

The 8 warm engine runs in the session logged **no** `dda_triton.model_readiness`
line at all — the gate saw `READY` and returned immediately. Its only trace on
the warm path is one native
`[triton_server.cpp:389] Model ... status is READY` line from the status read,
emitted 0.3 ms after `Resolved workflow model ...` and 19 ms before
`Setting pipeline to PLAYING state`. Warm run durations stayed at 0–1 s, matching
pre-fix runs. The last of them (`a9410c8d`, 04:29:20Z) came after ~12 minutes
idle and behaved the same — a per-process load state that is still `READY` costs
nothing.

## (d) Backend health across the session

`RestartCount=0`, `Status=running`, `Health=healthy`, `OOMKilled=false`,
`ExitCode=0` at the end, up continuously since 04:16:50Z and re-checked healthy
at 04:23:56Z and 04:29:04Z. All three restarts were deliberate; there was no
crash, no crash-loop and no automatic restart across 3 restarts and 12 runs (11
engine + 1 classic) spanning ~46 minutes. Longest unbroken window:
03:47:03Z→04:16:48Z (~30 min, 9 runs).

## (e) Observed cold-window durations — input to the deferred reconciler decision

All four cold observations (3 engine + 1 classic, two different models) reported
**3.0 s**, which is exactly `POLL_INTERVAL_S = 3.0`: the model was `READY` at the
gate's *first* poll after the kick. So 3.0 s is an upper bound on the real load
time, and the gate's own granularity — not the load — dominates what the operator
waits.

**Recommendation: keep the Triton boot-time reconciler deferred** (design.md
Decision 1). It could save at most one poll interval per backend restart on this
hardware, while the gate already removes all three reported harms. Revisit only
if a device reports a wait materially longer than one interval — e.g. the
original ONNX/TensorRT-build case the 600 s budget was sized for, which this
session did not reproduce.

## Incidental observation

Twice during the session the device's shadow read over IPC failed transiently and
every registration with binding points flipped to `invalid: bindings unavailable`
(04:04:43Z→~04:07:19Z, self-recovered; 04:11:40Z→04:16:50Z, cleared by the
deliberate restart). Unrelated to this fix — no changed file touches the shadow or
IPC path, and no workflow run failed because of it. Detail in the static-camera
spec's verification notes.
