# Bugfix Requirements Document

## Introduction

On jetson-thor1 (JP7/Thor, driver 595.78), nvargus/CSI ISP capture activity put
`nvargus-daemon` into a persistent degraded state that blocked ALL new CUDA context
creation device-wide: `cuCtxCreate` → CUDA_ERROR_OPERATING_SYSTEM (304) even as root,
`cudaSetDevice` → `cudaErrorDevicesUnavailable`, with the kernel signature
`Can't map dma attachment!` + `NVRM: GPU0 osCreateOsDescriptorFromFileHandle: Error
(89) while trying to import fd` appended exactly 1:1 per failed context creation
(200,273+ occurrences since boot). The fallout: the vLLM qwen deployment failed and
Greengrass rolled back the whole deployment, and all three ONNX vision models silently
degraded to CPU (ORT `CUDA → CPU` provider fallback reports READY) for a day.
`systemctl restart nvargus-daemon` cleared the state instantly — the poisoned state is
held by the daemon process, not by persistent kernel state. Onset was a discrete event
interleaved 1:1 with a `gst-launch nvarguscamerasrc num-buffers=1` single-frame capture
loop at ~0.5 s cadence. The authoritative evidence chain (hypothesis v3, the
nvargus-restart discriminator, and the clean-window re-test) lives in
`.kiro/specs/vllm-jp7-engine-cuda-init/bugfix.md`; the NVIDIA driver report is drafted
in `.kiro/specs/vllm-jp7-engine-cuda-init/nvidia-bug-report-draft.md`. This spec is the
DDA-side mitigation for that driver defect.

**User decision (binding):** most Thor devices have NO CSI camera; the only known CSI
configuration in the fleet is an Orin Nano running JP7. CSI/nvargus exposure must
therefore become opt-in. This spec covers three mitigations:

1. **nvargus-daemon disabled by default on devices without CSI** — an explicit opt-in
   flag in station provisioning (e.g. `ENABLE_CSI_CAMERA=1` for
   `station_install/setup_station.sh`), default OFF → `systemctl disable --now
   nvargus-daemon` on devices not opted in. Removes the poisoned-state holder entirely
   on the majority fleet.
2. **CSI capture redesign to a persistent pipeline** — the production CSI capture path
   is a while-true loop of SINGLE-FRAME `gst-launch nvarguscamerasrc num-buffers=1`
   captures: per-frame Argus session churn matching the incident onset signature 1:1
   (the worst-case trigger pattern). Redesign to ONE persistent capture pipeline
   feeding the existing staged-frame contract.
3. **Error(89) watchdog** — detect the kernel degraded-state signature and auto-restart
   `nvargus-daemon`, rate-limited and prominently logged so recovery is observable,
   never silent.

**What the investigation established about the current code (defect context):**

- The ACTIVE production CSI path is `nvidia-csi-capture.service` →
  `src/host_scripts/nvidia_csi_capture.sh`, which stages frames to
  `/aws_dda/nvidia-csi-capture/latest.jpg` (atomic `mv` from a temp file) and re-reads
  gain/exposure/crop from `/aws_dda/nvidia-csi-capture/config.json` every iteration.
  The backend consumes the staged FILE (`pipeline_builder._add_nvidia_csi_image_source`,
  the `csi_camera_source` workflow node mappings; JP6 additionally Pillow-decodes to a
  sibling `.dda_decoded.png` inside the backend). The config file is written solely by
  the backend (`workflow_engine/csi_capture.write_csi_config` and the legacy builder
  path). Manual exposure via `gainrange`/`exposuretimerange` (with `aeantibanding=0`,
  `wbmode=0`) is the validated control method per the NVIDIA_CSI_* notes.
- `src/host_scripts/start_csi_bridge.sh` (named pipe `/tmp/nvidia_csi_fifo`),
  `stop_csi_bridge.sh`, and `nvidia_csi_server.sh` (tcpserversink) have NO consumers
  anywhere in the codebase — they are legacy/dead experiment paths that use the same
  churn pattern. Their disposition is a design decision.
- `host_scripts/` are packaged INTO the LocalServer component by `build-custom.sh`
  (copied to `custom-build/$COMPONENT_NAME/host_scripts`), and every arm64 recipe
  (`recipe-arm64.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64-jp6.yaml`,
  `recipe-arm64-jp7.yaml`, `recipe.yaml`) runs
  `host_scripts/install_nvidia_csi_service.sh` UNCONDITIONALLY in the Install
  lifecycle — enabling and restarting `nvidia-csi-capture.service` on every arm64
  device at every deployment, camera or not. The amd64 recipes do not install it.
  Mitigation 2 therefore requires a LocalServer component build (~1–2 h, one at a
  time, security gate pre-checked) plus on-hardware verification per
  `.kiro/steering/builds.md`; it is NOT a host-script-only rollout.
- Nothing inside the container talks to Argus: the backend's CSI pipeline is a file
  source, NVIDIA_CSI camera status is never probed (`cameraStatus` is set to `None`),
  and the `deviceName: nvarguscamerasrc` entry in
  `default_camera_configurations.json` is metadata only. Disabling `nvargus-daemon`
  on a device without a CSI camera breaks nothing in DDA; only host-side
  `gst-launch nvarguscamerasrc` invocations touch the daemon.

**Scope guardrails:**

- `station_install/setup_station.sh` is PRESERVATION-TRACKED by the security gate: it
  is pinned byte-for-byte by
  `test/backend-test/security/preservation/test_preservation_dependency_setup_station.py`
  against `test/backend-test/security/baselines/dependency_baseline_setup_station.txt`
  (only the requests-pin version token may differ), and
  `baselines/dependency_baseline_unpinned_py36.json` records its unpinned install
  lines WITH line numbers, which shift when lines are added. Any edit for mitigation 1
  requires a conscious rebaseline of BOTH goldens in the same commit, with the
  preservation suite re-run green — the gate must never be weakened or deleted
  (`.kiro/steering/builds.md`).
- The arm64 recipes' Install scripts are pinned by test goldens in
  `test/backend-test/output_bindings_fixes/goldens/` and
  `test/backend-test/deploy_reliability/goldens/`; if the fix gates the recipe-driven
  CSI service install, those goldens need the same conscious update.
- Any change under `src/` (including `src/host_scripts/`) ships via the LocalServer
  component: full build per target, one at a time, security gate pre-checked, and
  on-hardware verification before commit, per `.kiro/steering/builds.md`.
- This spec does NOT change the vLLM or ONNX inference paths, cloud-side
  publish/packaging, or the NVIDIA bug report itself (tracked in the sibling spec).

**On-hardware verification note:** mitigations 1 and 3 are verifiable on jetson-thor1
without a CSI camera — the disable path needs no camera, and the watchdog can be
tested against the deliberate degraded-state reproduction session that the NVIDIA bug
report evidence chain needs anyway (synergy: one session serves both). Mitigation 2
genuinely needs a CSI-equipped device (the Orin Nano JP7 configuration); device
availability is an OPEN QUESTION for the user, flagged for the design phase.

**Open questions flagged for design (not resolved here):**

- Mechanism for keeping the CSI capture service inactive on non-opted-in devices given
  the recipes reinstall/restart it on every deployment: gate the recipe Install line
  (touches recipe goldens) vs. have the install/capture scripts self-gate on a
  provisioning-time marker (e.g. a flag file or the `nvargus-daemon` enabled state).
- Watchdog placement and scoping: host-side systemd unit/timer (shipped via
  host_scripts → component build) vs. backend-driven journal detection; opt-in-only
  vs. CSI-devices-only vs. all Jetson targets.
- How a persistent pipeline honors per-change gain/exposure/crop updates (Argus source
  properties cannot be changed mid-pipeline via gst-launch; a supervised pipeline
  restart on config change is one option) — the requirement below fixes the contract,
  not the mechanism.
- Disposition of the dead legacy scripts (`start_csi_bridge.sh`, `stop_csi_bridge.sh`,
  `nvidia_csi_server.sh`).

## Bug Analysis

### Current Behavior (Defect)

CSI/nvargus exposure is unconditional across the fleet, the shipped capture pattern is
the worst-case trigger for the driver defect, and nothing detects or recovers from the
degraded state:

1.1 WHEN a Jetson device is provisioned via `station_install/setup_station.sh` THEN the
system leaves `nvargus-daemon` in its JetPack default state (enabled and running) on
every device, with no opt-in or opt-out — including the majority of fleet devices that
have no CSI camera — keeping the process that holds the poisoned driver state resident
everywhere

1.2 WHEN a LocalServer component is deployed to any arm64 target (JP5, JP6, JP7, legacy
arm64) THEN the recipe Install lifecycle unconditionally runs
`host_scripts/install_nvidia_csi_service.sh`, which enables and restarts
`nvidia-csi-capture.service` on the host — on every deployment, with no check for a CSI
camera, no opt-in, and no relation to whether the device will ever use a CSI source
(Shutdown only stops it; the next deployment re-enables it)

1.3 WHEN `nvidia-csi-capture.service` runs THEN `nvidia_csi_capture.sh` executes a
while-true loop of SINGLE-FRAME captures (`gst-launch-1.0 nvarguscamerasrc
num-buffers=1 ... ! filesink`, then `sleep 0.1`) — creating and tearing down a full
Argus/ISP capture session for every frame at roughly the ~0.5 s cadence that matched
the jetson-thor1 incident onset signature 1:1; on devices with no CSI camera each
iteration still exercises the Argus connection path and fails silently
(stderr discarded to `/dev/null`)

1.4 WHEN `nvargus-daemon` enters the degraded state (kernel signature `Can't map dma
attachment!` + `NVRM: ... osCreateOsDescriptorFromFileHandle: Error (89)` accumulating
1:1 per failed CUDA context creation) THEN the system neither detects nor recovers from
it: ALL new CUDA context creation fails device-wide, ONNX vision models silently fall
back to CPU while reporting READY, vLLM engine loads fail, and the state persists
indefinitely until a human runs `systemctl restart nvargus-daemon`

1.5 WHEN the LocalServer component ships THEN it also carries the unreferenced legacy
capture scripts (`start_csi_bridge.sh` with its consumer-less `/tmp/nvidia_csi_fifo`,
`stop_csi_bridge.sh`, `nvidia_csi_server.sh`) that use the same per-frame or ad-hoc
Argus patterns if ever run manually

### Expected Behavior (Correct)

**Mitigation 1 — CSI/nvargus exposure is opt-in at provisioning:**

2.1 WHEN a device is provisioned via `station_install/setup_station.sh` WITHOUT the
explicit CSI opt-in flag (e.g. `ENABLE_CSI_CAMERA=1`; default OFF) THEN the system
SHALL disable and stop `nvargus-daemon` (`systemctl disable --now nvargus-daemon`),
removing the poisoned-state holder from the device

2.2 WHEN a device is provisioned WITH the CSI opt-in flag set THEN the system SHALL
leave `nvargus-daemon` enabled and running, and SHALL record the opt-in such that the
device is identifiable as CSI-enabled

2.3 WHEN a LocalServer component is deployed to an arm64 device that has NOT opted in
to CSI THEN the system SHALL NOT leave `nvidia-csi-capture.service` (or any other
nvargus-touching capture process) enabled and running — the per-frame Argus churn loop
SHALL NOT execute on non-opted-in devices, across repeated deployments (mechanism —
recipe gating vs. script self-gating on the provisioning marker — is a design decision)

2.4 WHEN any edit lands in `station_install/setup_station.sh` (or a gated recipe) for
this fix THEN the change SHALL include the conscious rebaseline of every affected
preservation golden in the same commit
(`dependency_baseline_setup_station.txt`, `dependency_baseline_unpinned_py36.json`,
and the recipe goldens if recipes change), with the preservation suite passing — the
security gate SHALL remain authoritative and SHALL NOT be weakened, skipped, or deleted

**Mitigation 2 — persistent capture pipeline on opted-in devices:**

2.5 WHEN a CSI-opted-in device captures frames THEN the system SHALL use ONE persistent
`nvarguscamerasrc` capture pipeline (a single long-lived Argus session) instead of a
per-frame create/teardown loop — eliminating the per-frame Argus session churn that
matches the driver-defect trigger pattern

2.6 WHEN the persistent pipeline runs THEN it SHALL preserve the existing staged-frame
contract unchanged for all consumers: frames staged to
`/aws_dda/nvidia-csi-capture/latest.jpg` as JPEG at the configured resolution
(3264x2464 default), atomically replaced (no partial reads), at a cadence at least
equivalent to the current loop (~0.5–1 s per frame or better)

2.7 WHEN the backend writes new acquisition settings to
`/aws_dda/nvidia-csi-capture/config.json` (gain, exposure, crop) THEN the capture
SHALL apply them using the validated manual-exposure method
(`gainrange`/`exposuretimerange` with `aeantibanding=0`, `wbmode=0`) and subsequent
staged frames SHALL reflect the new settings — the settings-change reactivity the
per-iteration config poll provides today SHALL be preserved (the mechanism for
applying settings to a persistent pipeline is a design decision)

2.8 WHEN the persistent capture pipeline fails (camera disconnect, Argus error, daemon
restart) THEN the capture service SHALL recover automatically (supervised restart)
without requiring a component redeployment, and SHALL log the failure visibly

**Mitigation 3 — Error(89) degraded-state watchdog:**

2.9 WHEN the kernel log accumulates the degraded-state signature (`Can't map dma
attachment!` together with `NVRM: ... osCreateOsDescriptorFromFileHandle: Error (89)
while trying to import fd`) beyond a defined detection threshold THEN the system SHALL
automatically restart `nvargus-daemon`, restoring device-wide CUDA context creation
without human intervention

2.10 WHEN the watchdog triggers a restart THEN it SHALL be rate-limited (a defined
minimum interval between automatic restarts, with escalation to a persistent visible
error if the signature recurs immediately) so a hard driver fault cannot cause a
restart loop

2.11 WHEN the watchdog takes any action (detection, restart, rate-limit suppression)
THEN it SHALL log prominently and durably (journal/service logs at warning level or
higher, identifying the signature counts and the action taken) so every automatic
recovery is observable and auditable — never a silent self-heal

2.12 WHERE the watchdog is deployed (opt-in-only, CSI-devices-only, or all Jetson
targets — a design decision) THEN on devices without the watchdog deployed the system
SHALL behave exactly as it does today

**Component hygiene — legacy script removal (traces defect 1.5; amendment per design
Decision 4):**

2.13 WHEN the LocalServer component is built THEN the component SHALL no longer ship
the legacy capture scripts (`start_csi_bridge.sh`, `stop_csi_bridge.sh`,
`nvidia_csi_server.sh`) — the consumer-less experiment paths carrying the same
per-frame Argus churn pattern

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a CSI-opted-in device (the Orin Nano JP7 configuration) runs CSI capture,
preview, image-source capture, and deployed `csi_camera_source` workflows THEN the
system SHALL CONTINUE TO deliver frames end-to-end through the existing consumer
contract: the backend file-source pipeline reading
`/aws_dda/nvidia-csi-capture/latest.jpg` (JP6's `.dda_decoded.png` staging included),
with no consumer-side changes required

3.2 WHEN a user adjusts CSI gain/exposure (and crop) through the backend THEN the
system SHALL CONTINUE TO write `/aws_dda/nvidia-csi-capture/config.json` via the
existing single write path (`csi_capture.write_csi_config`, tolerant of write
failures) and captured frames SHALL CONTINUE TO reflect manual exposure control per
the validated `gainrange`/`exposuretimerange` method

3.3 WHEN workflows or image sources use non-CSI camera paths (aravis/GigE `Camera`,
`ICam` v4l2 smart cameras, `Folder` sources, `aravis_camera_source` workflow nodes)
THEN the system SHALL CONTINUE TO capture and process frames exactly as before — none
of these paths touch nvargus and none SHALL be affected on any device, opted-in or not

3.4 WHEN a device is provisioned via `setup_station.sh` THEN the system SHALL CONTINUE
TO perform every existing provisioning step unchanged (argument handling, users/groups,
directories, Python 3.11 install with its pinned `requests==2.32.4` and the UNPINNED
system-python3 installs, GStreamer, Docker, Greengrass provisioning) — the CSI opt-in
block SHALL be purely additive

3.5 WHEN the LocalServer component deploys to any target THEN the system SHALL CONTINUE
TO run the existing health-gated lifecycle semantics (Install image loads, Startup
`up -d --wait`, Shutdown compose down) unchanged apart from the CSI service gating; the
amd64 recipes SHALL remain untouched

3.6 WHEN the security preservation gate runs (in the build and standalone) THEN it
SHALL CONTINUE TO pin `setup_station.sh` and the other tracked files against golden
baselines — goldens are consciously rebaselined for intended edits, and the gate's
tests SHALL CONTINUE TO exist and run unweakened

3.7 WHEN vision (ONNX/Triton) or vLLM inference runs on any device THEN the system
SHALL CONTINUE TO load and serve models exactly as before — this spec changes no
inference, model-handling, or container code paths

3.8 WHEN a device is healthy (no degraded-state signature in the kernel log) THEN the
watchdog SHALL CONTINUE TO leave `nvargus-daemon` untouched — zero automatic restarts,
zero interference with an opted-in device's live CSI capture
