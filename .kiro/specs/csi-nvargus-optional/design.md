# CSI/nvargus Optional Bugfix Design

## Overview

On jetson-thor1 (JP7/Thor, driver 595.78), nvargus/CSI ISP capture activity put
`nvargus-daemon` into a persistent degraded state that blocked ALL new CUDA
context creation device-wide (`cuCtxCreate` → CUDA_ERROR_OPERATING_SYSTEM (304),
kernel signature `Can't map dma attachment!` + `NVRM: ... 
osCreateOsDescriptorFromFileHandle: Error (89)` appended 1:1 per failed context
creation). The fallout was a rolled-back vLLM deployment and a full day of the
three ONNX vision models silently degraded to CPU. The driver defect itself goes
to NVIDIA (sibling spec `vllm-jp7-engine-cuda-init`); this spec is the DDA-side
mitigation, and the DDA-side defect is real and three-fold:

1. **Exposure is unconditional.** Every arm64 recipe unconditionally runs
   `host_scripts/install_nvidia_csi_service.sh` in its Install lifecycle,
   enabling and restarting `nvidia-csi-capture.service` on every deployment on
   every Jetson — camera or not — and provisioning leaves `nvargus-daemon` in
   its JetPack default (enabled) everywhere. The poisoned-state holder is
   resident on the whole fleet even though most Thor devices have no CSI camera.
2. **The shipped capture pattern is the worst-case trigger.**
   `nvidia_csi_capture.sh` is a while-true loop of SINGLE-FRAME
   `gst-launch-1.0 nvarguscamerasrc num-buffers=1` captures at ~0.5 s cadence —
   a full Argus session create/teardown per frame, matching the incident onset
   signature 1:1.
3. **Nothing detects or recovers.** When the degraded state hits, ALL new CUDA
   context creation fails device-wide indefinitely until a human runs
   `systemctl restart nvargus-daemon` — while ORT reports READY on CPU.

The fix follows the binding user decision (CSI becomes opt-in) with three
mitigations, engineered to keep recipe and golden churn at zero where possible:

- **Mitigation 1 (opt-in provisioning):** `station_install/setup_station.sh`
  gains a purely additive, appended `ENABLE_CSI_CAMERA` block (default OFF):
  without the flag it disables `nvargus-daemon` and removes the opt-in marker;
  with it, it enables the daemon and writes the marker
  `/aws_dda/system/csi_camera_optin`. The setup_station goldens are consciously
  rebaselined in the same commit.
- **Mitigation 2 (persistent pipeline):** `nvidia_csi_capture.sh` is rewritten
  around ONE persistent `nvarguscamerasrc` pipeline (single long-lived Argus
  session) with a supervised restart on config change — preserving the
  staged-frame contract (`/aws_dda/nvidia-csi-capture/latest.jpg`, atomic
  replace) and the validated manual-exposure method unchanged for all consumers.
- **Mitigation 3 (Error(89) watchdog):** a new host-side systemd oneshot
  service + timer (`nvargus-error89-watchdog.{service,timer}` +
  `nvargus_error89_watchdog.sh`) detects the kernel degraded-state signature
  and restarts `nvargus-daemon`, rate-limited and prominently logged. It is
  installed on ALL Jetson targets by the existing installer.

The keystone design decision (Decision 1 below) is that the per-deployment
gating lives in `install_nvidia_csi_service.sh` itself (self-gating on the
provisioning marker), NOT in the recipes: the five arm64 recipes stay
byte-identical, so the ~10 recipe golden JSONs across
`output_bindings_fixes/goldens/` and `deploy_reliability/goldens/` need no
rebaseline, and the opt-in decision lives at provisioning time where the
knowledge ("does this station have a CSI camera?") actually exists.

All shipped changes live in `src/host_scripts/` (LocalServer component → full
per-target builds, one at a time, security gate pre-checked, on-hardware
verification per `.kiro/steering/builds.md`) plus the additive
`setup_station.sh` block (ships with `station_install/`, no component build,
but preservation-gate rebaseline required). No recipe, compose, container,
backend, or inference-path code changes. amd64 recipes and images are untouched
by construction (they never invoke the installer).

Mitigations 1 and 3 are verifiable on jetson-thor1 without a camera — and the
watchdog verification is designed to double as the deliberate degraded-state
reproduction session the NVIDIA bug report needs (one session, two outputs).
Mitigation 2 needs the CSI-equipped Orin Nano JP7 and is a gated, user-scheduled
verification; the rollout is decoupled so mitigations 1 and 3 can land fleet-wide
without it (the rewritten capture script only executes on opted-in devices, and
the only opted-in device is the Orin itself).

## Glossary

- **Bug_Condition (C)**: a Jetson (arm64) device state in which CSI/nvargus
  exposure exists without opt-in (nvargus-daemon resident and/or
  `nvidia-csi-capture.service` enabled by deployment on a non-opted-in device),
  or an opted-in device runs the per-frame Argus session churn loop, or the
  Error(89) degraded-state signature accumulates with no automatic recovery
- **Property (P)**: the desired behavior — no nvargus-touching process runs on
  a non-opted-in device across repeated deployments; opted-in capture uses one
  persistent Argus session honoring the staged-frame and settings contracts;
  the degraded state is detected and auto-recovered, rate-limited and loudly
  logged
- **Preservation**: the opted-in end-to-end CSI consumer contract (backend file
  source on `latest.jpg`, JP6 `.dda_decoded.png` staging, `write_csi_config`),
  all non-CSI camera paths, every existing `setup_station.sh` provisioning
  step, recipe lifecycle semantics, the security preservation gate itself, and
  inference (ONNX/Triton/vLLM) on every device
- **nvargus-daemon**: the NVIDIA Argus camera daemon (host systemd unit); the
  process that held the poisoned driver state on jetson-thor1 — restarting it
  cleared the state instantly
- **Argus session**: a camera capture session brokered by nvargus-daemon; the
  unfixed loop creates and tears one down per frame
- **Error(89) signature**: the kernel degraded-state fingerprint — `Can't map
  dma attachment!` together with `NVRM: GPU0 osCreateOsDescriptorFromFileHandle:
  Error (89) while trying to import fd`, appended 1:1 per failed CUDA context
  creation
- **Opt-in marker**: `/aws_dda/system/csi_camera_optin` — flag file written by
  `setup_station.sh` when `ENABLE_CSI_CAMERA=1`; the provisioning-time record
  that a station legitimately uses a CSI camera (requirement 2.2), and the gate
  the installer checks on every deployment
- **`ENABLE_CSI_CAMERA`**: environment variable consumed by `setup_station.sh`
  (same convention as its existing `VERBOSE`); default `0`/unset = no CSI
- **`nvidia-csi-capture.service`**: host systemd unit running
  `/aws_dda/system/nvidia_csi_capture.sh`; installed/enabled today by every
  arm64 recipe's Install lifecycle via `install_nvidia_csi_service.sh`
- **Staged-frame contract**: frames staged as JPEG to
  `/aws_dda/nvidia-csi-capture/latest.jpg` (3264x2464 default), atomically
  replaced via `mv` so consumers never read a partial file; acquisition
  settings read from `/aws_dda/nvidia-csi-capture/config.json` (gain, exposure,
  crop), written solely by the backend (`csi_capture.write_csi_config`)
- **Validated manual-exposure method**: `gainrange="$G $G"`
  `exposuretimerange="$E $E"` with `aeantibanding=0`, `wbmode=0` on
  `nvarguscamerasrc` (per NVIDIA_CSI_SETTINGS_FIX.md /
  NVIDIA_CSI_EXPOSURE_TROUBLESHOOTING.md)
- **Watchdog**: `nvargus_error89_watchdog.sh` driven by
  `nvargus-error89-watchdog.timer` → `.service` (oneshot); scans the kernel
  journal incrementally (cursor file), restarts `nvargus-daemon` on signature
  accumulation, rate-limited with escalation
- **Recipe goldens**: parsed-recipe-structure fixtures in
  `test/backend-test/output_bindings_fixes/goldens/` and
  `test/backend-test/deploy_reliability/goldens/` that pin the arm64/amd64
  recipes' Install/Startup/Shutdown blocks byte-identically
- **Security preservation gate**: `test/backend-test/security/preservation/`
  suite pinning `setup_station.sh` byte-for-byte
  (`dependency_baseline_setup_station.txt`, only the requests-pin version token
  may differ) and recording its unpinned system-python3 install lines WITH line
  numbers (`dependency_baseline_unpinned_py36.json`)
- **jetson-thor1**: JP7/Thor verification device, no CSI camera; **Orin Nano
  JP7**: the fleet's only known CSI-equipped configuration

## Bug Details

### Bug Condition

The bug manifests on any arm64 Jetson device in three compounding ways: (a) the
device carries live nvargus exposure it never asked for — provisioning leaves
`nvargus-daemon` enabled everywhere and every LocalServer deployment re-enables
and restarts `nvidia-csi-capture.service` unconditionally; (b) where capture
does run, it runs as per-frame Argus session churn (`num-buffers=1` loop,
~0.5 s cadence) — the trigger pattern matching the incident onset 1:1; and
(c) when the driver defect fires, the degraded state persists indefinitely with
no detection and no recovery while inference silently degrades.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X of type DeviceState
         { arch: Arch, csiOptIn: boolean, deployments: int,
           captureMode: none | perFrameChurn | persistent,
           nvargusDaemonResident: boolean,
           error89SignatureAccumulating: boolean,
           autoRecovered: boolean }
  OUTPUT: boolean

  RETURN X.arch IN {arm64JP5, arm64JP6, arm64JP7, arm64legacy}
         AND (
           // (a) exposure without opt-in, re-created by every deployment
           (NOT X.csiOptIn AND X.deployments >= 1
                AND csiCaptureServiceEnabled(X))
           OR (NOT X.csiOptIn AND X.nvargusDaemonResident)
           // (b) worst-case trigger pattern on the capture path
           OR (X.captureMode = perFrameChurn)
           // (c) degraded state with no automatic recovery
           OR (X.error89SignatureAccumulating AND NOT X.autoRecovered)
         )
END FUNCTION
```

On the unfixed tree every arm64 device satisfies C(X): the recipes make
`csiCaptureServiceEnabled` true after any deployment regardless of `csiOptIn`
(which does not even exist as a concept), the only shipped capture mode IS
`perFrameChurn`, and `autoRecovered` is always false (no watchdog exists).

### Examples

- **jetson-thor1 incident (the motivating event)**: no CSI camera, yet
  `nvidia-csi-capture.service` enabled by deployment; a single-frame capture
  loop at ~0.5 s cadence ran against nvargus; from a discrete onset event the
  Error(89) signature accumulated 200,273+ times, ALL new CUDA context creation
  failed device-wide for ~a day, the vLLM qwen deployment rolled back, and
  three ONNX vision models served from CPU while reporting READY. Recovery
  required a human `systemctl restart nvargus-daemon`. Expected: no capture
  service on a camera-less device, and even if the state arises (any nvargus
  use), automatic detection and restart within minutes.
- **Any non-CSI Jetson, every deployment (defect 1.2)**: deploy the LocalServer
  component → Install runs `install_nvidia_csi_service.sh` → service enabled
  and restarted; Shutdown stops it; the NEXT deployment re-enables it. Expected
  (2.3): a non-opted-in device ends every deployment with the service disabled
  and inactive.
- **Camera-less churn (defect 1.3)**: on a device with no camera the loop still
  exercises the Argus connection path every ~0.6 s and discards stderr
  (`2>/dev/null`) — silent, endless, invisible. Expected: no loop at all
  (non-opted-in), and visible logging wherever capture does run.
- **Opted-in churn (defect 1.3 on the Orin Nano)**: even the legitimate CSI
  station pays one full Argus session create/teardown per frame — roughly
  100,000+ sessions/day of exactly the pattern that preceded the incident.
  Expected (2.5): ONE persistent session in steady state.
- **Edge case — settings change on an opted-in device**: the unfixed loop picks
  up `config.json` changes on the next iteration (~1 s). The fixed persistent
  pipeline must preserve that reactivity (2.7) even though Argus source
  properties cannot be changed mid-pipeline via gst-launch.
- **Edge case — healthy device (must NOT change, 3.8)**: no Error(89) lines in
  the kernel journal → the watchdog must take zero actions, forever.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- The opted-in CSI consumer contract end-to-end (3.1, 3.2): backend file-source
  pipeline reading `/aws_dda/nvidia-csi-capture/latest.jpg` (JP6's
  `.dda_decoded.png` staging included), `csi_capture.write_csi_config` as the
  single tolerant config write path, frames reflecting the validated
  `gainrange`/`exposuretimerange` manual-exposure method. **No file under
  `src/backend/` changes in this spec.**
- All non-CSI camera paths (3.3): aravis/GigE `Camera`, `ICam` v4l2, `Folder`
  sources, `aravis_camera_source` workflow nodes — none touch nvargus.
- Every existing `setup_station.sh` provisioning step (3.4): the CSI block is
  purely additive, appended at the END of the file so the recorded line numbers
  of the unpinned system-python3 install lines (656, 680 in
  `dependency_baseline_unpinned_py36.json`) do not shift.
- Recipe lifecycle semantics (3.5): Install image loads, Startup
  `up -d --wait` health gating, Shutdown compose down + `systemctl stop
  nvidia-csi-capture || true` — all five arm64 recipes stay byte-identical;
  amd64 recipes untouched.
- The security preservation gate (3.6): its tests continue to exist and run
  unweakened; goldens are consciously rebaselined for the intended
  `setup_station.sh` edit in the same commit.
- Vision (ONNX/Triton) and vLLM inference on every device (3.7): no inference,
  model-handling, or container code paths change.
- Healthy-device watchdog neutrality (3.8): zero signature → zero restarts,
  zero interference with an opted-in device's live capture.

**Scope:**
All inputs that do NOT involve the CSI/nvargus exposure surface are completely
unaffected. This includes every amd64 device, every non-CSI camera workflow,
every inference request, the container images, compose files, and recipes
(byte-identical), and — on devices where the watchdog observes a clean kernel
journal — `nvargus-daemon` itself.

## Hypothesized Root Cause

> Not actually a hypothesis in this spec: unlike the sibling spec, the
> investigation completed before requirements were written. Section header
> kept per the bugfix design format.

Unlike the sibling spec, no hypothesis is needed here: the investigation record
in bugfix.md (and the evidence chain in
`.kiro/specs/vllm-jp7-engine-cuda-init/bugfix.md`) established the causes
directly. Stated for the record:

1. **Unconditional recipe-driven install** (defect 1.2): all five arm64 recipes
   run `host_scripts/install_nvidia_csi_service.sh` in Install with no
   condition; the installer unconditionally copies the capture script, enables
   and restarts the service. There is no concept of CSI opt-in anywhere in
   provisioning or deployment. Shutdown's `systemctl stop` is undone by the
   next deployment's Install.
2. **Per-frame session churn by design** (defect 1.3): `nvidia_csi_capture.sh`
   was built from single-frame test commands (see FINAL_SOLUTION.md and the
   root-level experiment scripts) and inherited their `num-buffers=1` shape as
   a production loop. Each iteration is a full Argus session create/teardown
   against nvargus-daemon — the exact cadence and pattern interleaved 1:1 with
   the jetson-thor1 incident onset.
3. **No degraded-state handling anywhere** (defect 1.4): nothing in DDA reads
   the kernel journal for the Error(89) signature; the backend cannot see it
   (and would itself be crash-looping or rolled back exactly when it matters);
   ORT's CUDA→CPU fallback masks the damage as READY.
4. **Dead code with the dangerous pattern still ships** (defect 1.5):
   `start_csi_bridge.sh`, `stop_csi_bridge.sh`, `nvidia_csi_server.sh` have no
   consumers but carry the same churn loop, inviting manual reuse during
   debugging — precisely the kind of ad-hoc capture activity that poisoned
   jetson-thor1.

## Design Decisions

The five open questions from bugfix.md, investigated and decided.

### Decision 1 — Deployment gating mechanism: installer self-gating on the provisioning marker (NOT recipe gating)

**Decision:** `install_nvidia_csi_service.sh` self-gates on the opt-in marker
`/aws_dda/system/csi_camera_optin` written by `setup_station.sh`. Marker absent
→ the installer runs `systemctl disable --now nvidia-csi-capture.service` and
skips the capture install; marker present → the existing install path runs
unchanged. The five arm64 recipes are not edited.

**Rationale:**
- Recipe gating multiplies: it touches 5 recipe files AND rebaselines ~10
  golden JSON fixtures across three suites (`output_bindings_fixes` full
  goldens; `deploy_reliability` structure goldens AND defect-E baselines — all
  verified to pin the Install script text byte-identically). Installer
  self-gating touches ZERO recipes and ZERO recipe goldens: the recipes keep
  invoking the same script; the script's behavior changes.
- The decision belongs at provisioning time. "Does this station have a CSI
  camera?" is knowledge the operator has when running `setup_station.sh`, not
  something a per-target recipe can express (recipes are per-JetPack, not
  per-station; the Orin Nano JP7 and jetson-thor1 would share `recipe-arm64-jp7.yaml`).
- Reach: legacy devices provisioned before this fix have no marker, so the very
  next component deployment disables the capture service fleet-wide — no
  re-provisioning required for mitigation of the churn trigger.
- **Rejected alternative — keying off `systemctl is-enabled nvargus-daemon`:**
  JetPack ships nvargus-daemon enabled by default, so every legacy device would
  read as "opted in" and the fleet-wide default-OFF goal would silently fail.
  The explicit marker has exactly the right default (absent = OFF) and
  satisfies 2.2's "record the opt-in" requirement directly.
- Deliberately conservative reach split: the deployment-time gate handles ONLY
  `nvidia-csi-capture.service` (per requirement 2.3); disabling
  `nvargus-daemon` itself remains a provisioning-time action (2.1). Legacy
  devices therefore keep an idle nvargus-daemon until re-provisioned — with the
  churn source removed it holds no poisoned state, and the watchdog (Decision
  2) covers the residual risk from any manual nvargus use.

### Decision 2 — Watchdog placement and scope: host-side systemd timer, ALL Jetson targets

**Decision:** the watchdog is a host-side systemd oneshot service + timer
(`nvargus-error89-watchdog.service` / `.timer`, script
`nvargus_error89_watchdog.sh`), shipped in `host_scripts/` and installed by the
existing `install_nvidia_csi_service.sh` on EVERY arm64 target, opted-in or not.

**Rationale — placement (host-side, not backend-driven):**
- The watchdog must work exactly when the backend cannot: the incident's blast
  radius included a Greengrass deployment rollback, and jetson-thor1 carries a
  pre-existing awscrt crash loop — a backend-driven detector dies with its
  host. Detection is journal reading, not CUDA, so nothing requires the
  container; a host systemd timer is independent of container health, survives
  crash loops, rollbacks, and compose down, and starts at boot.
- The backend container has no kernel-journal access today; granting it one
  would be a compose/container change this spec otherwise avoids entirely.
- Installing via the EXISTING installer invocation means zero recipe edits
  (Decision 1's economics again): the same unconditional Install hook that was
  the problem becomes the distribution channel for the fix.
**Rationale — scope (all-Jetson, not CSI-only or opt-in-only):**
- The signature cannot false-positive on a healthy device (requirement 3.8's
  premise): a healthy kernel journal contains zero
  `osCreateOsDescriptorFromFileHandle: Error (89)` lines, so on the
  non-degraded fleet the watchdog is a cheap periodic journal scan that never
  acts.
- The degraded state does not require the capture service: ANY nvargus contact
  (a manual `gst-launch nvarguscamerasrc`, a diagnostic script, a future
  feature) can trigger the driver defect. Non-opted-in devices with a resident
  legacy nvargus-daemon are exactly the ones where silent CPU fallback would
  otherwise return.
- Scoping it opt-in-only would protect the one device least likely to need
  unattended recovery (the CSI station is actively used and observed) and leave
  the majority fleet unprotected.

### Decision 3 — Persistent pipeline mechanism: supervised gst-launch with restart-on-config-change (bash, no new host dependencies)

**Decision:** rewrite `nvidia_csi_capture.sh` as a bash supervisor around ONE
long-lived `gst-launch-1.0 nvarguscamerasrc` pipeline (no `num-buffers`). The
supervisor polls `config.json` every second; on a gain/exposure/crop change it
terminates the pipeline and relaunches it with the new settings (one Argus
session teardown per human settings change). Frame staging uses `multifilesink`
with an index pattern plus an atomic-mv stager, preserving the never-a-partial-
read contract.

**Rationale:**
- **vs. a python/gst app with dynamic property setting:** nvarguscamerasrc's
  exposure/gain properties are not reliably changeable on a PLAYING pipeline
  (the validated manual-exposure method was established with launch-time
  properties), so even a python app would restart the source element to apply
  settings — the reactivity win is marginal. Meanwhile a python-gi/gst app adds
  HOST-side dependencies (`python3-gi`, GIR bindings) that `setup_station.sh`
  does not install today, expanding provisioning scope and its preservation
  goldens for no contract improvement. Bash + gst-launch is what runs today,
  what the NVIDIA_CSI notes validated, and what field operators can debug.
- **Churn arithmetic:** today ~1.5 sessions/second forever (~130k/day). Fixed:
  1 session at service start + 1 per settings change (human-driven, rare) + 1
  per failure recovery. That is a >100,000x reduction in Argus session churn on
  a quiet day, and the settings-change restart is the same operation the daemon
  handles for any camera app exit.
- **Contract compliance:** the staged `latest.jpg` remains present and complete
  throughout a settings-change restart (consumers read the last staged frame
  during the ~2–3 s gap; 2.6's "no partial reads" holds absolutely, and steady-
  state cadence improves from ~0.6–1 s/frame to a configured 2 fps). New
  settings take effect within ~poll interval + pipeline start (~2–3 s),
  preserving the ~1 s-class reactivity the per-iteration poll provides today
  (2.7).
- **Failure recovery (2.8):** the supervisor relaunches the pipeline with
  backoff and VISIBLE logging when it dies (camera disconnect, Argus error,
  daemon restart) — stderr is no longer discarded; systemd `Restart=always`
  still supervises the supervisor itself.

### Decision 4 — Dead legacy scripts: REMOVE them

**Decision:** delete `src/host_scripts/start_csi_bridge.sh`,
`stop_csi_bridge.sh`, and `nvidia_csi_server.sh`.

**Rationale:** they have no consumers anywhere in the codebase (verified in the
bugfix investigation and re-checked); no test golden or baseline pins them
(the recipe goldens reference only `install_nvidia_csi_service.sh`,
`compose_lifecycle.sh`, `setup_dda_users.sh`, `get_nvidia_libs_versions.sh`,
`configure_usb.sh`; the security preservation suite pins only
`get_nvidia_libs_versions.sh`'s decision block among host scripts); the recipes
touch host scripts via a `chmod .../host_scripts/*.sh` glob, which is deletion-
safe. They embody the exact per-frame churn pattern this spec exists to
eliminate, and retention invites the manual-debugging reuse that is precisely
the incident's trigger class. They were never copied to `/aws_dda/system` by
the installer, so no on-device cleanup is needed — the files simply stop
shipping in the next component artifact. **Baseline/golden implications: none**
(verified as above); `build-custom.sh` copies the directory wholesale, so no
build-script edit is needed either.

**Note for the requirements record:** defect 1.5 names these scripts but the
Expected Behavior section has no numbered clause mandating their removal; this
design maps the removal to defect 1.5 directly and flags the gap as a candidate
requirements amendment (a 2.13 "the component SHALL no longer ship the legacy
capture scripts") for traceability.

### Decision 5 — Root-level experiment scripts: out of scope, note only

**Decision:** the repo-root experiment scripts (`test_final_solution.sh`,
`test_ae_lock.sh`, `test_nvargus_params.sh`, `test_working_exposure.sh`,
`simple_csi_test.sh`, and the diagnostics `check_csi_service.sh` /
`check_where_running.sh` / `diagnose_gstreamer.sh`) are NOT shipped in the
component and are NOT modified by this spec. They are the historical
investigation record behind the validated exposure method (referenced by
FINAL_SOLUTION.md and the NVIDIA_CSI_*.md notes). Cleanup is a housekeeping
note, not a requirement: if they are ever touched, the single worthwhile change
is a header warning that single-frame `nvarguscamerasrc` loops are the
driver-defect trigger pattern and must not be run repeatedly against production
devices. Recorded in the Cross-Spec Documentation Consistency table; no task
will modify them.

## Correctness Properties

Property 1: Bug Condition - CSI/nvargus Exposure Is Opt-In and Churn-Free

_For any_ arm64 Jetson device where the bug condition holds (isBugCondition
returns true — unconditional CSI service enablement across deployments, default
nvargus-daemon residency without opt-in, or the per-frame Argus session churn
capture pattern), the fixed tree SHALL eliminate the condition: provisioning
without `ENABLE_CSI_CAMERA=1` disables nvargus-daemon and records no opt-in;
every deployment to a non-opted-in device ends with `nvidia-csi-capture.service`
disabled and inactive (installer self-gate on the absent marker, stable across
repeated deployments); provisioning WITH the flag records the opt-in marker and
leaves nvargus-daemon enabled; and the shipped capture script contains no
per-frame `num-buffers=1` session loop — it launches one persistent pipeline.

**Validates: Requirements 2.1, 2.2, 2.3, 2.5**

Property 2: Preservation - Everything Outside the CSI Exposure Surface Is Unchanged

_For any_ input where the bug condition does NOT hold (opted-in CSI capture
consumers, non-CSI camera paths, existing provisioning steps, recipe lifecycle
semantics, the security gate, inference, healthy-device watchdog neutrality),
the fixed tree SHALL produce the same result as the original tree: all five
arm64 recipes and both amd64 recipes byte-identical; `setup_station.sh`
byte-identical except the appended CSI block (unpinned py36 golden line numbers
unshifted); no file under `src/backend/` modified; the staged-frame contract
(path, JPEG, 3264x2464, atomic mv, config.json keys, validated manual-exposure
parameters) preserved verbatim in the rewritten capture script; and a watchdog
observing a signature-free kernel journal SHALL take zero actions.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8**

Property 3: Fix Checking - Persistent Pipeline Honors the Staging and Settings Contracts

_For any_ sequence of config.json settings writes (gain, exposure, crop) and
pipeline lifecycle events (start, failure, recovery) on an opted-in device, the
fixed capture service SHALL stage complete JPEG frames to
`/aws_dda/nvidia-csi-capture/latest.jpg` via atomic rename at a cadence at
least equivalent to the unfixed loop, SHALL apply each settings change via the
validated manual-exposure method through exactly one supervised pipeline
restart (subsequent staged frames reflect the new settings), and SHALL recover
from pipeline death automatically with visible logging — while creating no more
than one Argus session per (start | settings change | recovery), never one per
frame.

**Validates: Requirements 2.5, 2.6, 2.7, 2.8**

Property 4: Fix Checking - Watchdog Detects, Recovers, Rate-Limits, and Logs

_For any_ kernel journal stream, the fixed watchdog SHALL restart
`nvargus-daemon` if and only if the new-since-last-scan Error(89) signature
count meets the detection threshold AND nvargus-daemon is active AND the
minimum interval since the last automatic restart has elapsed; SHALL suppress
(and loudly log) restarts inside the rate-limit window, escalating to a
persistent visible error when the signature recurs immediately after restarts;
and SHALL emit a journal-visible warning-or-higher log line identifying the
signature counts and the action taken for EVERY action (detection, restart,
suppression) — with zero actions of any kind on a signature-free stream.

**Validates: Requirements 2.9, 2.10, 2.11, 2.12**

Property 5: Fix Checking - Golden Rebaseline Discipline

_For any_ preservation-tracked file this fix edits (`setup_station.sh`), the
same commit SHALL rebaseline every affected golden
(`dependency_baseline_setup_station.txt` regenerated byte-for-byte;
`dependency_baseline_unpinned_py36.json` verified — line numbers unshifted by
the end-append, updated only if shifted) with the preservation suite passing,
and SHALL NOT weaken, skip, or delete any gate test; no recipe golden changes
because no recipe changes.

**Validates: Requirements 2.4, 3.6**

## Fix Implementation

### Changes Required

**File 1 — `station_install/setup_station.sh` (Mitigation 1; edit, appended block)**

Append a self-contained CSI opt-in block at the END of the file (after the
GreengrassV2TokenExchangeRole section). End placement is deliberate: the
unpinned-py36 golden records line numbers (656, 680) for earlier lines, and an
append does not shift them. The block follows the file's existing tolerant
style (`run_cmd` + `add_warning`, never hard-failing the setup):

```bash
echo "=========================================="
echo "▶ Configuring CSI camera exposure (opt-in)"
echo "=========================================="
# CSI/nvargus is OPT-IN (spec: csi-nvargus-optional). nvargus/CSI ISP activity
# can poison nvargus-daemon on JP7/Thor (driver 595.78) into a state where ALL
# new CUDA context creation fails device-wide (kernel: "Can't map dma
# attachment!" + NVRM osCreateOsDescriptorFromFileHandle Error (89)). Most
# fleet devices have NO CSI camera, so the daemon is disabled unless the
# operator provisions with ENABLE_CSI_CAMERA=1. The opt-in marker gates the
# nvidia-csi-capture.service install at every component deployment.
CSI_OPTIN_MARKER="/aws_dda/system/csi_camera_optin"
if systemctl list-unit-files nvargus-daemon.service >/dev/null 2>&1; then
    mkdir -p /aws_dda/system
    if [ "${ENABLE_CSI_CAMERA:-0}" = "1" ]; then
        echo "ENABLE_CSI_CAMERA=1 — enabling nvargus-daemon and recording CSI opt-in"
        run_cmd "systemctl enable --now nvargus-daemon" || add_warning "Failed to enable nvargus-daemon"
        printf 'enabled_by=setup_station.sh\nenabled_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$CSI_OPTIN_MARKER" \
            || add_warning "Failed to write CSI opt-in marker $CSI_OPTIN_MARKER"
        echo "✓ CSI camera opt-in recorded at $CSI_OPTIN_MARKER"
    else
        echo "No CSI opt-in (default) — disabling nvargus-daemon and clearing any opt-in marker"
        run_cmd "systemctl disable --now nvargus-daemon" || add_warning "Failed to disable nvargus-daemon"
        rm -f "$CSI_OPTIN_MARKER"
        echo "✓ nvargus-daemon disabled (re-run with ENABLE_CSI_CAMERA=1 to opt in)"
    fi
else
    echo "✓ nvargus-daemon not present on this device — no CSI configuration needed"
fi
echo ""
```

Notes:
- `ENABLE_CSI_CAMERA` is an environment variable, matching the script's
  existing `VERBOSE` convention; the positional `<aws-region> <thing_name>`
  contract is untouched (3.4).
- The `list-unit-files` guard makes the block a no-op on non-Jetson devices
  (amd64 stations have no nvargus-daemon).
- Re-provisioning WITHOUT the flag consciously opts a device OUT (2.1's
  "WITHOUT the flag → disable"), including clearing a stale marker.

**File 2 — `src/host_scripts/install_nvidia_csi_service.sh` (Mitigations 1+3; edit)**

Two changes: gate the capture-service install on the opt-in marker, and always
install/refresh the watchdog. The script keeps `set -e` with explicit `|| true`
where systemd state may legitimately vary:

```bash
CSI_OPTIN_MARKER="/aws_dda/system/csi_camera_optin"

# --- Error(89) watchdog: installed on ALL Jetson targets (opt-in or not) ---
# Detection is journal-based and cannot false-positive on a healthy device;
# the degraded state can arise from ANY nvargus contact, not just the capture
# service (spec: csi-nvargus-optional, Decision 2).
cp "$SCRIPT_DIR/nvargus_error89_watchdog.sh" /aws_dda/system/
chmod +x /aws_dda/system/nvargus_error89_watchdog.sh
cp "$SCRIPT_DIR/nvargus-error89-watchdog.service" /etc/systemd/system/
cp "$SCRIPT_DIR/nvargus-error89-watchdog.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nvargus-error89-watchdog.timer

# --- CSI capture service: gated on the provisioning-time opt-in marker ---
if [ ! -f "$CSI_OPTIN_MARKER" ]; then
    echo "CSI camera not opted in ($CSI_OPTIN_MARKER absent) — ensuring capture service is off"
    systemctl disable --now nvidia-csi-capture.service 2>/dev/null || true
    echo "nvidia-csi-capture.service disabled (provision with ENABLE_CSI_CAMERA=1 to opt in)"
    exit 0
fi

# (existing install path, unchanged semantics: jq install, copy capture
#  script, install unit, daemon-reload, enable, restart)
```

Idempotency: every deployment converges the device to the marker's state —
marker absent → service disabled+inactive (2.3, stable across repeated
deployments); marker present → service refreshed and running (3.1).

**File 3 — `src/host_scripts/nvidia_csi_capture.sh` (Mitigation 2; rewrite)**

Persistent-pipeline supervisor. Shape (constants tunable at the top of the
script):

```bash
FRAMERATE_NUM=2          # staged-frame cadence target (2 fps; unfixed ~1 fps)
CONFIG_POLL_INTERVAL=1   # seconds between config.json polls
RESTART_BACKOFF=5        # seconds before relaunch after a pipeline failure
STAGE_PATTERN="$CAPTURE_DIR/stage_%05d.jpg"

launch_pipeline() {      # one persistent Argus session; NO num-buffers
    # Validated manual-exposure method preserved verbatim:
    # aeantibanding=0 wbmode=0 exposuretimerange="$E $E" gainrange="$G $G"
    gst-launch-1.0 -e \
        nvarguscamerasrc sensor_id=0 \
        aeantibanding=0 wbmode=0 \
        exposuretimerange="$EXPOSURE $EXPOSURE" \
        gainrange="$GAIN $GAIN" ! \
        'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
        nvvidconv ! 'video/x-raw,format=BGRx' ! videoconvert ! \
        videorate drop-only=true ! "video/x-raw,framerate=${FRAMERATE_NUM}/1" ! \
        $CROP_PARAMS \
        jpegenc idct-method=2 quality=100 ! \
        multifilesink location="$STAGE_PATTERN" max-files=3 &
    GST_PID=$!
}

stage_frames() {         # atomic staging: index N is complete once N+1 exists
    # newest-but-one stage file -> mv (atomic, same fs) -> latest.jpg
    # preserves the never-a-partial-read contract exactly as the old
    # temp.jpg -> mv -> latest.jpg did
}

# Main: read_config (existing jq logic, unchanged keys/defaults) -> launch ->
# loop every CONFIG_POLL_INTERVAL:
#   - stage_frames
#   - re-read config; if gain/exposure/crop changed: log the change, TERM the
#     pipeline, relaunch with new settings (ONE session per settings change)
#   - if the pipeline died on its own: log LOUDLY (stderr NOT discarded),
#     sleep RESTART_BACKOFF, relaunch (2.8); systemd Restart=always remains
#     the outer supervisor
```

What is preserved verbatim from the unfixed script (Property 2 leans on this):
the capture directory, config path and keys (`gain`, `exposure`,
`crop.top/bottom/left/right`), jq-based `read_config` with the same defaults
(gain 4, exposure 5000000), the default-config bootstrap, the videocrop
construction, resolution/caps `3264x2464 framerate=21/1`, `jpegenc
idct-method=2 quality=100`, atomic `mv` staging to `latest.jpg` with
`chmod 666`, and the validated manual-exposure parameter set. What is removed:
`num-buffers=1`, the per-iteration `gst-launch` invocation, `sleep 0.1`, and
every `2>/dev/null` on the capture path.

**Files 4–6 — NEW `src/host_scripts/nvargus_error89_watchdog.sh`, `nvargus-error89-watchdog.service`, `nvargus-error89-watchdog.timer` (Mitigation 3)**

Watchdog script contract (constants at top; all testable host-side with
stubbed `journalctl`/`systemctl`/`logger` — see Testing Strategy):

```bash
SIG_NVRM='osCreateOsDescriptorFromFileHandle.*Error (89)'
SIG_DMA="Can't map dma attachment"
SIG_THRESHOLD=3            # new signature lines per scan to trigger
RESTART_MIN_INTERVAL=600   # seconds between automatic restarts (2.10)
ESCALATION_WINDOW=3600     # if >= ESCALATION_COUNT restarts in this window,
ESCALATION_COUNT=3         #   suppress + log persistent visible error
STATE_DIR=/var/lib/dda/nvargus-watchdog   # cursor file + restart history
LOG_TAG=nvargus-error89-watchdog

# Scan: journalctl -k --cursor-file "$STATE_DIR/cursor" (incremental — each
# line is counted exactly once across scans; first run seeds the cursor).
# Trigger: new_count(SIG_NVRM) >= SIG_THRESHOLD AND dma-attachment signature
# present in the same window.
# Guard:   only restart if `systemctl is-active --quiet nvargus-daemon`
#          (a stopped/disabled daemon holds no poisoned state to clear).
# Action:  logger -t $LOG_TAG -p daemon.err "detected degraded-state
#          signature: N new Error(89) lines ... restarting nvargus-daemon";
#          systemctl restart nvargus-daemon; record restart epoch.
# Rate-limit: inside RESTART_MIN_INTERVAL -> NO restart; log suppression at
#          daemon.warning with counts (2.10, 2.11).
# Escalation: >= ESCALATION_COUNT restarts within ESCALATION_WINDOW -> stop
#          restarting; log a persistent daemon.err every scan naming the
#          condition ("hard driver fault suspected — automatic restarts
#          suppressed; manual intervention required").
# Healthy stream: zero matches -> exit 0 silently (3.8; no journal spam).
```

Units: `.service` is `Type=oneshot` running the script; `.timer` is
`OnBootSec=2min`, `OnUnitActiveSec=1min`, `Persistent=true`, wanted by
`timers.target`. Detection latency is therefore ≤ ~1 minute per scan cycle
after signature accumulation reaches threshold. The oneshot+timer shape (vs a
daemon) means a crashed scan affects one cycle only and the script re-reads
state fresh each run.

**File 7 — DELETE `src/host_scripts/start_csi_bridge.sh`, `src/host_scripts/stop_csi_bridge.sh`, `src/host_scripts/nvidia_csi_server.sh` (Decision 4)**

No consumers, no golden pins, recipes reference host scripts by glob. The files
stop shipping with the next component build.

**File 8 — Baselines: `test/backend-test/security/baselines/dependency_baseline_setup_station.txt` (regenerate), `dependency_baseline_unpinned_py36.json` (verify)**

Same commit as File 1 (requirement 2.4 / Property 5): regenerate the full-file
golden byte-for-byte from the fixed `setup_station.sh`; verify the unpinned
py36 entries still resolve at lines 656/680 (they must — the block is
appended), updating only if shifted. Run the preservation suite in the
flask-app container per `.kiro/steering/builds.md` before any build.

**File 9 — NEW test suite `test/backend-test/csi_nvargus_optional/`**

Exploration, preservation, and fix-check tests (see Testing Strategy). Includes
a baseline goldens capture of the five arm64 recipes' parsed structure to make
"recipes byte-identical" an executable assertion rather than a claim.

**Explicitly NOT changed:** all recipe YAMLs, `src/docker-compose.yaml`, all
Dockerfiles, anything under `src/backend/` or `src/frontend/`,
`build-custom.sh`, `nvidia-csi-capture.service` (the unit file — same
ExecStart path), the amd64 recipes, and the root-level experiment scripts
(Decision 5).

## Cross-Spec Documentation Consistency

| Document | Relationship to this fix | Action |
|---|---|---|
| `.kiro/specs/vllm-jp7-engine-cuda-init/bugfix.md` | Authoritative evidence chain for the driver defect (hypothesis v3, nvargus-restart discriminator, clean-window re-test); names this spec as the mitigation follow-up | No change — remains the incident-evidence authority |
| `.kiro/specs/vllm-jp7-engine-cuda-init/nvidia-bug-report-draft.md` | The NVIDIA driver report; needs the deliberate degraded-state reproduction evidence that Verification Session A produces, and should note the DDA-side mitigations exist | Update AFTER Session A: attach reproduction evidence; add a one-line mitigation note referencing this spec (task in tasks.md) |
| `.kiro/specs/vllm-jp7-engine-cuda-init/design.md` | Its original Hypothesized Root Cause called the Error(89) dmesg spam "driver noise independent of this bug" — superseded by the re-hypothesis recorded in its own bugfix.md; retained there unmodified per house style | No change (the sibling's re-scope banner already redirects readers) |
| `.kiro/specs/model-gpu-fallback-visibility/bugfix.md` | Complementary: makes the silent ORT CUDA→CPU fallback visible — the observability gap that let this incident run for a day. No mechanism overlap (backend-side vs host-side) | No change; note the complementary coverage here |
| `NVIDIA_CSI_SETUP.md` | Describes the capture service install and the per-frame test command; the service-architecture description becomes stale after Mitigation 2, and it does not mention opt-in | Update after fix: persistent-pipeline description + `ENABLE_CSI_CAMERA=1` provisioning prerequisite (task) |
| `NVIDIA_CSI_SETTINGS_FIX.md`, `NVIDIA_CSI_EXPOSURE_TROUBLESHOOTING.md` | Source of the validated manual-exposure method (`gainrange`/`exposuretimerange`, `aeantibanding=0`, `wbmode=0`) — the fixed pipeline preserves it verbatim | No change — still authoritative for the exposure method |
| `FINAL_SOLUTION.md`, `PRACTICAL_SOLUTION.md`, `RUN_THESE_TESTS.md` | Historical investigation records documenting the `num-buffers=1` pattern that became the production loop | No change (historical); superseded operationally by this design |
| Root experiment/diagnostic scripts (`test_*.sh`, `simple_csi_test.sh`, `check_csi_service.sh`, `check_where_running.sh`, `diagnose_gstreamer.sh`) | Not shipped; historical/diagnostic (Decision 5). Single-frame capture loops in them are the trigger pattern | No change; housekeeping note only — do not run capture loops repeatedly against production devices |
| `.kiro/steering/builds.md` | Process authority for builds, the security gate, and on-hardware verification; this design's plans are written to comply with it | No change |

## Deployment and On-Hardware Verification

### Rollout shape and scheduling

1. **No builds until the pending JP7 build (vllm-jp7-engine-cuda-init) finishes**
   — builds run strictly one at a time (`pgrep -af "gdk component build"` /
   `pgrep -af "build-custom.sh"` before dispatching anything).
2. **Pre-build gate (builds.md, verbatim discipline):** rebaseline
   `dependency_baseline_setup_station.txt` (and verify
   `dependency_baseline_unpinned_py36.json`) in the same commit as the
   `setup_station.sh` edit; run the guard suite
   (`test_preservation_out_of_scope_guard.py`,
   `test_preservation_secrets_out_of_scope_guard.py`) and the preservation
   suite in the flask-app container; move `cdk.out` aside; no portal deploys
   during the build.
3. **Build order:** JP7 first (`aws.edgeml.dda.LocalServer.arm64JP7`,
   ~1–2 h, log to `.gdk_build_jp7.log`) — jetson-thor1 is the verification
   device. **Scheduling note:** where sensible, share the JP7 build cycle with
   other pending device-side changes (e.g. anything queued from sibling specs)
   so the fleet takes one component version bump, not several. JP5/JP6 builds
   follow sequentially when scheduled; the change is identical shell in
   `host_scripts/` for all targets.
4. **Decoupled CSI rollout:** the general fleet can take this component version
   with only Sessions A-level verification, because the rewritten capture
   script cannot execute on any non-opted-in device (the installer disables the
   service before it would run). Deployment TO the CSI-equipped Orin Nano is
   gated on Session B.
5. **Provisioning-side change (`setup_station.sh`)** ships with the repo, not
   the component; it takes effect per-device at the next provisioning run. Its
   verification is part of Session A.

### Session A — jetson-thor1 (JP7, no camera): Mitigations 1 + 3, plus NVIDIA-report evidence

This session deliberately reproduces the degraded state ONCE, because the
NVIDIA bug report needs that evidence anyway — the watchdog verification and
the report reproduction are the same session (synergy noted in bugfix.md).

1. **Pre-deploy capture:** record `systemctl status nvargus-daemon
   nvidia-csi-capture`, `systemctl list-timers`, journal cursor position,
   driver version, and a working `cuCtxCreate` probe (baseline healthy state).
2. **Deploy the JP7 component.** Assert: `nvidia-csi-capture.service` is
   `disabled` + `inactive` (no marker on thor1); `nvargus-error89-watchdog.timer`
   is `enabled` + active with the `.service` firing cleanly on a healthy
   journal (zero actions logged — 3.8); backend healthy; vision models on GPU.
3. **Redeploy (second deployment).** Assert the capture service is STILL
   disabled+inactive — the repeated-deployment invariant of 2.3 that the old
   Install/Shutdown cycle violated.
4. **Degraded-state reproduction (watchdog timer temporarily stopped):**
   `systemctl start nvargus-daemon` (thor1 will have it enabled pre-fix;
   adjust to actual state), then run the churn loop — the UNFIXED
   `num-buffers=1` single-frame capture at ~0.5 s cadence — until the kernel
   signature appears. Capture for the NVIDIA report: dmesg/journal signature
   excerpts with timestamps, the failing `cuCtxCreate` → 304 probe, driver
   version, onset correlation with the churn loop. (This is the documented
   trigger; if the state does not reproduce in a bounded session, record that
   honestly and proceed — the watchdog can be exercised against a synthetic
   journal only in host tests, so hardware validation of detection would then
   wait for a natural occurrence.)
5. **Watchdog recovery:** restart the watchdog timer. Within ≤ ~1 timer cycle
   assert: detection log line (counts + action) at warning-or-higher in the
   journal (2.11); `nvargus-daemon` restarted automatically (2.9); the
   `cuCtxCreate` probe succeeds again — device-wide CUDA recovery with no
   human `systemctl` invocation.
6. **Rate-limit and escalation:** immediately re-run the churn loop to
   re-poison. Assert the watchdog logs suppression (no second restart) inside
   `RESTART_MIN_INTERVAL` (2.10); after the interval, the next restart
   proceeds. If practical in the session, drive three rapid cycles to observe
   the escalation log; otherwise the escalation path rests on the host-side
   behavioral tests (honesty guard below).
7. **Provisioning verification:** run `sudo ./setup_station.sh <region>
   <thing>` WITHOUT the flag — assert nvargus-daemon disabled+stopped, no
   marker, all pre-existing steps still green (idempotent re-run, 3.4). Run
   again with `ENABLE_CSI_CAMERA=1` — assert daemon enabled+running and marker
   written (2.1, 2.2). Finish by restoring thor1's default posture: no marker,
   nvargus-daemon disabled, watchdog timer enabled.
8. **Sustained health:** leave the device for a sustained period (builds.md:
   not just at startup); confirm no watchdog false positives, no backend
   crash-loop changes attributable to this deployment.

**Evidence bundle:** journal excerpts, systemctl states, probe outputs, and the
reproduction timeline — filed to the NVIDIA report draft and to this spec's
verification record.

### Session B — Orin Nano JP7 + CSI camera (GATED, user-scheduled): Mitigation 2

Requires the only CSI-equipped device; scheduled by the user when available.
Until then, the component MUST NOT be deployed to that device (decoupling rule
above).

1. Provision with `ENABLE_CSI_CAMERA=1`; deploy the component. Assert the
   capture service is enabled+running and the marker present.
2. **Single-session invariant (2.5):** exactly one `gst-launch-1.0
   nvarguscamerasrc` process, stable PID over minutes; nvargus logs show one
   session, not one per frame.
3. **Staging contract (2.6):** `latest.jpg` mtime advances at ≥ the unfixed
   cadence; a tight reader loop (decode every observed `latest.jpg`) finds
   zero partial/corrupt JPEGs over a sustained window.
4. **Settings reactivity (2.7):** change gain/exposure (and crop) through the
   backend; assert `config.json` written by the existing path, exactly one
   pipeline restart in the service log, and staged frames visibly reflecting
   the new exposure within ~3 s.
5. **Failure recovery (2.8):** `systemctl restart nvargus-daemon` mid-capture
   and (if physically practical) a camera unplug/replug; assert the supervisor
   logs the failure visibly and staging resumes without redeployment.
6. **End-to-end preservation (3.1, 3.2):** run CSI preview, image-source
   capture, and a deployed `csi_camera_source` workflow; assert frames flow
   through the unchanged backend consumer contract.
7. Sustained-health soak per builds.md before calling it done.

### JP5 / JP6 follow-on

The same installer/watchdog ships to JP5/JP6 at their next component builds.
Per builds.md's every-arch rule, each rollout needs an on-device smoke on that
arch: capture service disabled after deploy (no marker), watchdog timer active
and quiet on a healthy journal. The gating logic is target-independent shell,
so the JP7-validated behavior plus these smokes is the honest coverage claim.

## Testing Strategy

### Validation Approach

Two phases per the bugfix methodology: first surface counterexamples on the
UNFIXED tree (exploration tests written to assert the FIXED expectation, so
they FAIL on unfixed code and become the fix-check suite once the fix lands),
alongside preservation tests that PASS on the unfixed tree and must keep
passing. Everything below is host-runnable and GPU-free EXCEPT where the
honesty guard says otherwise. New suite:
`test/backend-test/csi_nvargus_optional/`.

### Exploratory Bug Condition Checking

**Goal**: demonstrate each leg of the bug condition on the UNFIXED tree.

**Test plan**: text-level assertions against the shipped scripts/recipes, plus
behavioral tests that execute the real shell scripts with stubbed system
binaries (`systemctl`, `journalctl`, `gst-launch-1.0`, `logger` on `PATH` —
the same stubbing pattern `deploy_reliability`'s stub-docker tests use).

**Test cases (all FAIL on unfixed code — this confirms the bug):**
1. **Installer is unconditional**: `install_nvidia_csi_service.sh` contains a
   marker-gated disable path (`csi_camera_optin` +
   `systemctl disable --now nvidia-csi-capture`) — unfixed script has neither.
2. **Behavioral gate check**: run the real installer with a stub `systemctl`
   and no marker; assert `disable --now nvidia-csi-capture.service` was called
   and `enable`/`restart` of the capture service was NOT — unfixed installer
   enables+restarts unconditionally.
3. **Capture script is per-frame churn**: `nvidia_csi_capture.sh` contains no
   `num-buffers=1` and launches a persistent pipeline (single `gst-launch`
   invocation outside any per-frame loop; `multifilesink`/staging supervisor
   present; no `2>/dev/null` on the capture command) — unfixed script fails on
   every clause.
4. **No provisioning opt-in**: `setup_station.sh` handles `ENABLE_CSI_CAMERA`
   with both the `systemctl disable --now nvargus-daemon` default branch and
   the marker-writing opt-in branch — absent on unfixed.
5. **No watchdog exists**: `host_scripts/` contains
   `nvargus_error89_watchdog.sh` + `.service` + `.timer`, with the script
   matching both signature patterns (`Error (89)`, `Can't map dma attachment`)
   and the installer enabling the timer — all absent on unfixed.

**Expected counterexamples** (documented when the suite runs on unfixed code):
the unconditional `systemctl enable/restart` in the installer; the
`num-buffers=1 ... 2>/dev/null` loop body; the absence of any
`ENABLE_CSI_CAMERA`/marker/watchdog artifact. These are the textual fingerprints
of defects 1.1–1.5.

### Fix Checking

**Goal**: for all inputs where the bug condition holds, the fixed tree produces
the expected behavior.

**Pseudocode:**
```
FOR ALL X WHERE isBugCondition(X) DO
  result := deploy_or_provision_or_scan_fixed(X)
  ASSERT noUnrequestedExposure(result) AND persistentPipeline(result)
         AND autoRecovery(result)
END FOR
```

**Test cases (the exploration suite above, now passing, PLUS):**
1. **Installer idempotency across deployments**: run the fixed installer twice
   with no marker (stubbed systemctl) → capture service disabled both times,
   watchdog timer enabled both times; then with marker → capture install path
   runs (jq check, copy, enable, restart) exactly as the unfixed script did.
2. **Watchdog behavioral suite** (stubbed `journalctl` serving canned/generated
   streams, stubbed `systemctl`/`logger` recording invocations), property-based
   with hypothesis where natural (the repo already uses it):
   - _For any_ journal stream with ≥ SIG_THRESHOLD new signature lines and an
     active daemon and no recent restart → exactly one
     `systemctl restart nvargus-daemon` + a warning-or-higher log naming counts
     and action (2.9, 2.11).
   - _For any_ stream meeting the threshold within RESTART_MIN_INTERVAL of a
     recorded restart → zero restarts + a suppression log (2.10).
   - Escalation: ≥ ESCALATION_COUNT restarts inside ESCALATION_WINDOW →
     restarts stop, persistent error logged every scan (2.10).
   - Inactive/disabled daemon → no restart attempted.
   - Cursor discipline: the same journal lines are never counted twice across
     consecutive scans.
3. **Capture supervisor behavioral test** (stub `gst-launch-1.0` that records
   its argv and fakes stage-file production): initial launch uses config.json's
   gain/exposure via the validated parameter set; a config change produces
   exactly one kill+relaunch with the new argv; a stub that exits nonzero
   produces a visible log line and a backoff relaunch; `latest.jpg` is only
   ever produced by `mv` from a complete stage file (atomicity leg — assert no
   direct writes to `latest.jpg`).
4. **setup_station block** (text-level only — the full script cannot be safely
   executed host-side): both branches present, marker path exact, `run_cmd`/
   `add_warning` tolerance style, `list-unit-files` guard present, block
   strictly appended after the last unfixed line.

### Preservation Checking

**Goal**: for all inputs where the bug condition does NOT hold, fixed behavior
equals unfixed behavior.

**Pseudocode:**
```
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

**Test plan (observation-first: capture baselines from the UNFIXED tree, then
assert them against the fixed tree; these tests PASS on unfixed code):**
1. **Recipes byte-identical**: capture the parsed structure of all five arm64
   recipes + both amd64 recipes on the unfixed tree into suite-local goldens;
   assert equality after the fix. (The existing `output_bindings_fixes` and
   `deploy_reliability` golden suites double-cover this — they must stay green
   untouched, which is itself the assertion that no recipe golden was
   rebaselined.)
2. **setup_station prefix property**: every line of the unfixed
   `setup_station.sh` (all 1625, modulo the one allowed requests-pin token) is
   byte-identical and IN THE SAME POSITION in the fixed file; the CSI block is
   strictly appended. Corollary asserted directly: the
   `dependency_baseline_unpinned_py36.json` entries still match their recorded
   line numbers (656, 680).
3. **Staged-frame contract fingerprint**: the fixed capture script preserves
   the contract constants verbatim — capture dir, `latest.jpg`, `config.json`
   keys and jq defaults (gain 4, exposure 5000000, crop 0s), 3264x2464 caps,
   `jpegenc idct-method=2 quality=100`, `aeantibanding=0`, `wbmode=0`,
   `exposuretimerange`/`gainrange`, atomic `mv` + `chmod 666`, videocrop
   construction.
4. **Backend untouched**: `git diff --name-only` scope guard — no file under
   `src/backend/`, `src/frontend/`, `src/docker-compose.yaml`, Dockerfiles, or
   recipe YAMLs is modified by this spec's commits (executable as a test that
   hashes the CSI consumer files — `csi_capture.py`, `pipeline_executor.py`,
   `pipeline_builder.py`, `catalog/nodes.py` — against unfixed-tree hashes).
5. **Watchdog neutrality (3.8), property-based**: _for any_ generated journal
   stream containing zero signature lines (arbitrary benign kernel noise,
   including near-miss lines with "Error (89)" absent or dma-attachment text
   alone), the watchdog performs zero systemctl calls and writes zero
   warning/error logs.
6. **Security gate integrity (3.6)**: the preservation suite files themselves
   are unmodified (no weakened/deleted tests) and the suite passes against the
   rebaselined goldens in the flask-app container (run per builds.md).

### Unit Tests

- Installer gate: marker present/absent/unreadable; systemd unit missing.
- Watchdog: threshold boundary (2 vs 3 new lines), cursor seeding on first
  run, state-file corruption tolerance, both-signatures-required conjunction.
- Capture supervisor: config diff detection for each key (gain, exposure, each
  crop edge), crop-params construction, default-config bootstrap.

### Property-Based Tests

- Watchdog action function over generated journal streams (fix-check 2 and
  preservation 5 above) — the strongest preservation guarantee in the suite.
- Config-change sequences: _for any_ sequence of config.json writes, the
  supervisor performs exactly one relaunch per effective change and zero for
  no-op rewrites of identical values.

### Integration Tests

- Full installer run under a fake root (`DESTDIR`-style stubbing) for both
  marker states, asserting the complete systemd interaction transcript.
- Supervisor lifecycle: launch → fake frames staged → config change → relaunch
  → fake crash → backoff relaunch, asserting the `latest.jpg` staging stream
  never gaps to a partial file.
- On-hardware Sessions A and B (above) are the real integration tier.

### Honesty Guard — what host tests CANNOT prove

Everything above runs GPU-free on the build host with stubbed system binaries.
The following are ONLY provable on hardware, and the verification plan assigns
each to a session:

- Real Argus session behavior — that ONE `nvarguscamerasrc` session stays
  healthy for hours and that per-frame churn is actually gone at the daemon
  level (Session B, steps 2–3).
- Real frames: exposure/gain/crop visibly applied by the persistent pipeline
  (Session B, step 4). No stub can validate optics.
- Real kernel-journal detection: that the deployed watchdog sees the actual
  Thor signature text and its restart genuinely clears device-wide CUDA
  context creation (Session A, steps 4–5; the instant-clear effect of a daemon
  restart is already evidenced once, manually, in the sibling spec's record).
- systemd semantics on-device: timer persistence across reboots, unit
  interactions with JetPack's own nvargus-daemon unit (Sessions A/B).
- `setup_station.sh` full-run behavior on a real device (Session A, step 7) —
  host tests are text-level only for this file.
- JP5/JP6 device behavior — identical shell, but per builds.md it is not
  "done" on those arches until their follow-on smokes run on real JP5/JP6
  devices.
- The escalation path under a genuine hard driver fault — Session A exercises
  it opportunistically (step 6); otherwise it rests on the stubbed behavioral
  tests, stated here plainly.
