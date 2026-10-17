# Verification notes: triton-model-cleanup-wedge (JP5)

Device: the MIC-730 (JP5, Jetson Xavier, Triton 2.45.0). Verified
2026-10-16/17 on the built and published LocalServer, before the device-side
commits. JP6 (Jetson AGX Orin), JP7 (Jetson AGX Thor) and x86 (the x86
workstation) were built from the committed branch and verified on 2026-10-17;
see "Other targets (task 14)".

## Deviation (device verification, 2026-10-16)

> **Criterion 2.4 is partially met on JP5**, agreed with the owner.
>
> - Verified: each stub start is bounded at 10 s, and the patched
>   `libtriton_python.so` is in the image (both sentinel strings present).
> - Not met: when `base_<model>/<v>/model.py` is missing, the load relaunches
>   the stub about every 10 s inside one load call (43 relaunches in 7.5 min).
>   The model stays `LOADING` instead of going `UNAVAILABLE`, and other
>   models' loads requested meanwhile stay blocked.
> - Mitigations observed: self-heal, on by default, restarts the backend after
>   15 min of such a loop; restoring `model.py` makes the same load reach
>   `READY` in 32-79 s.
>
> The loop is carried by `.kiro/specs/triton-python-stub-relaunch-loop/bugfix.md`,
> with the log excerpts and native stacks captured on the device.

## Build and publish

- Pre-build gate (task 7): every suite of the task 2.1 baseline ran on the
  build source, host, in-image (pinned flask-app image) and under Python 3.8
  (133 passed). `comm -13` against the baseline printed nothing for any
  suite. The guard suite was green, `cdk.out` was absent, and no Portal deploy
  ran.
- JP5 build (`TARGETS="5" ./run_jp_builds.sh`, recipe-arm64-jp5.yaml copied
  over recipe.yaml): exit 0 in 28 min. The python_backend layer ran the
  clone, the `git apply` of `edgeml-triton-python-backend.diff`, the source
  `grep` sentinel and the `libtriton_python.so` binary sentinel.
- **Process deviation (owner-accepted):** the build ran with
  `SKIP_BACKEND_TESTS=1`. The in-image stream-camera gate
  (`test/backend-test/stream_ingest/test_image_stream_components.py`, the
  H.265 RTMP sample muxed with libx265) hangs on this 48-CPU build host when
  the container is not CPU-limited (reproduced 3 times, unrelated to this
  change; it passes in under 1 s with 8 CPUs). Instead, the full gate body of
  `build-custom.sh` was replayed on the freshly built flask-app image with
  `--cpuset-cpus=0-7` before publishing: exit 0, with every gate's "passed"
  line (stream camera, injection/deserialization with the full preservation
  suite 142 passed / 10 skipped, secrets/JWT, IAM, S3 squatting, Docker base
  image, dependency CVE). The replay log is kept with the build evidence. The
  hang is a follow-up for its own bugfix.
- Published: `aws.edgeml.dda.LocalServer.arm64JP5` **1.0.56**, immediately
  after the build, with no other build running and both image ids unchanged.
  The cloud recipe has `aws.greengrass.Cli` with `DependencyType: SOFT` and
  `DefaultConfiguration.ModelLoadSelfHealEnabled: "true"`.
- The committed content equals the built source: the `git diff HEAD --binary`
  hash and the untracked source files' sha256 were re-checked after the device
  work.

## Fixed LocalServer deployment

Copy of the device's deployment with only the LocalServer version changed
(1.0.54 -> 1.0.56), no model component added, workflows idle. COMPLETED in
7.5 min.

- The backend runs the new image. The host copies of `model_convertor.py`,
  `convert_model_cleanup.py`, `dda_triton/model_locks.py`,
  `model_status_records.py` and `repo_entries.py` equal the source by sha256.
- Watchdog start line:
  `Triton model-load watchdog started (self-heal on (ModelLoadSelfHealEnabled=true); GPU load signal /sys/devices/platform/17000000.gv11b/load; stall after 15m, at most 3 restarts per 24h and 30m apart)`.
- The shadow carries `modelReadiness` and `selfHeal` (`enabled: true`;
  `/feature-configurations/gpu-status` also shows `disabledBy: null`).
- Rollout caveat, as designed: the cascade's model Shutdowns ran the
  **previous** cleanup script (`StopModel: Request failed with status code: 403`
  for UNKNOWN models, then `Cleaned directory` x3), and the Startups ran the
  new convertor (`Model <entry> changed; publishing the new entry`). All four
  models were READY about 5 min after the backend started.
- 15 min sustained health: /health 200 every minute, no container restart.

## R1: cleanup during a load — PASS

Backend restarted, all models UNKNOWN; `/start` for the yolo-test model, its
cleanup 1 s later. Cleanup output (exit 0, 14 s):

```
INFO:Release model-yolo-test-jetson-xavier-jp5: backend reachable, state LOADING
INFO:Release model-yolo-test-jetson-xavier-jp5: a load is in flight (state LOADING); waiting up to 300s for it to finish before removing its repository entries
INFO:Release model-yolo-test-jetson-xavier-jp5: load finished (READY); unloading before removing its repository entries
INFO:Model stopped successfully!
INFO:Release model-yolo-test-jetson-xavier-jp5: unloaded (state UNAVAILABLE)
INFO:Cleaned directory: /aws_dda/dda_triton/triton_model_repo/base_model-yolo-test-jetson-xavier-jp5
INFO:Cleaned directory: /aws_dda/dda_triton/triton_model_repo/marshal_model-yolo-test-jetson-xavier-jp5
INFO:Cleaned directory: /aws_dda/dda_triton/triton_model_repo/model-yolo-test-jetson-xavier-jp5
INFO:Directory cleanup finished
```

No `pb_stub` missing-file line in the backend log. Two other models were READY
31 s after their `/start`. The component restart re-converted yolo-test:
READY in 25 s.

## R3: native probe — partially met (see Deviation)

Scratch entries `model-r3-probe` (copies of the yolo-test entries, names
renamed inside each `config.pbtxt`), `base_model-r3-probe/8/model.py`
removed.

- Run 1: `/start`; no `UNAVAILABLE` within 30 s. The backend log has
  `pb_stub.cc:2081] Failed to preinitialize Python stub: Python model file not found in '/aws_dda/dda_triton/triton_model_repo/base_model-r3-probe/8/model.py'`
  every ~10 s, 43 times in 7.5 min, each from a new stub; the model stayed
  `LOADING`. Restoring `model.py`: READY 32 s later.
- Run 2: a second model's `/start` 15 s into the loop stayed `LOADING` for
  more than 5 min; after the restore both were READY within 79 s.
- The guarded cleanup removed the scratch entries both times; no `.trash-*` or
  `.staging-*` remained.

## R2: today's deployment shape — PASS

Deployment adding one unused, published JP5 model component and merging
`{"ModelLoadSelfHealEnabled": "true"}` into LocalServer: COMPLETED in 1.5 min.

- The Greengrass log has no `Restarting service because dependency aws.greengrass.Cli was in a bad state`
  line (0 `bad state` lines), the backend did not restart, and no other model
  component cycled.
- The new component's Startup: `Model ... changed; publishing the new entry`
  x3, READY in 25 s, exit 0. Every model READY.
- One PPE demo run triggered afterwards completed.
- Rollback (the previous revision with a LocalServer `reset` of
  `/ModelLoadSelfHealEnabled`): the removed component's Shutdown ran the
  guarded cleanup (`backend reachable, state READY`, `load finished (READY); unloading ...`,
  `unloaded (state UNAVAILABLE)`, `Cleaned directory` x3,
  `Directory cleanup finished`), no backend restart, 0 `bad state` lines.
- 15 min sustained health after each of the two deployments.

## Self-heal (design steps 0-6) and F3

Probe `model-selfheal-probe`: a python model whose `initialize()` blocks on
`threading.Event().wait()`.

- **Step 0 — PASS.** No override, no kill file, no record, no run in flight,
  the GPU node read 0 for one minute.
- **Step 1, the shipped default acts — PASS.** `/start` at 00:43:10Z.

  ```
  00:58:14 WARNING Triton model model-selfheal-probe is stalled: LOADING for 15m04s with no load progress for 32m12s; evidence: [stall] the Triton model index has not changed for 32m11s and the loading models' stub processes used 0.1 CPU-s in the last 5m (1 stub process(es) alive). Recovery: Self-heal is restarting the LocalServer backend (attempt 1 of 3 in 24 h).
  00:58:14 INFO    SELF-HEAL quiescing: the Triton model load is wedged (models LOADING: model-selfheal-probe for 15m03s (suspected culprit)); pausing continuous workflows and waiting up to 60s for 0 run(s) not blocked by the wedge to finish; new workflow runs count as in flight
  00:58:15 ERROR   SELF-HEAL: restarting the LocalServer backend to clear a wedged Triton model load. Models LOADING: model-selfheal-probe for 15m04s (suspected culprit). Evidence: [stall] the Triton model index has not changed for 32m12s and the loading models' stub processes used 0.1 CPU-s in the last 5m (1 stub process(es) alive). Action: backend process restart (attempt 1 of at most 3 per 24h, at least 30m apart; 0 run(s) blocked by the wedge are interrupted, 0 of them to be marked failed; vLLM models dropped: none). Record: /aws_dda/dda_triton/model_status/self_heal.json
  00:58:26 INFO    SELF-HEAL: the backend restarted after self-heal attempt 1 (requested 2026-10-17T00:58:15Z); record: /aws_dda/dda_triton/model_status/self_heal.json
  ```

  Exactly one `SELF-HEAL:` ERROR line. The container restarted at 00:58:17
  (restart count 0 -> 1). `self_heal.json` holds attempt 1 with `restartedAt`.
  `lastSelfHeal` appeared on the probe in `/feature-configurations`, and
  `selfHeal.last` in the shadow and in the Portal `GET /devices/{id}`
  response. No other automatic restart happened during the whole verification.
- **Step 2, the per-device opt-out — PASS (by the cloud merge; see Findings).**
  A cloud configuration merge `{"ModelLoadSelfHealEnabled": "false"}`: the
  shadow reported `enabled: false`, `disabledBy: "config"` 16 s after the
  deployment completed. Re-wedged probe, after 15 min:

  ```
  01:24:08 WARNING Triton model model-selfheal-probe is stalled: LOADING for 15m17s ... Recovery: Self-heal is off on this device (ModelLoadSelfHealEnabled=false). Restart the LocalServer backend: LocalServer UI, Application health overview, Application logs and restart; or on the device `sudo docker restart <backend container>`.
  01:24:08 WARNING Triton model load wedge detected (models LOADING: model-selfheal-probe for 15m17s (suspected culprit); evidence: ...); self-heal is off on this device (ModelLoadSelfHealEnabled=false). Restart the LocalServer backend manually.
  ```

  `loadHealth.state` `stalled` with that recovery action, `selfHeal.state`
  `disabled`, no restart.
- **Step 3, the kill file — PASS (NIT, see Findings).** Kill file created, then
  the key reset by a cloud deployment: `disabledBy: "file"`, the recovery
  action names `/aws_dda/dda_triton/model_status/self-heal.disabled`, no
  restart.
- **F3 — PASS.** Hand-run convertor Startup for a scratch component
  `model-f3-probe` (a copy of the yolo-test artifact, queued behind the
  probe), after 6.3 min:

  ```
  WARNING:StartModel: model-f3-probe is not READY at the end of its Startup (state LOADING, 3 attempts over 378s): still LOADING after 3 start attempts over 378 s; the load continues in the backend. Recorded the outcome in /aws_dda/dda_triton/model_status/model-f3-probe.json; the Startup still exits 0 so the deployment is not rolled back.
  ```

  Exit code 0; `loadOutcome` in `/feature-configurations`, the model in the
  shadow's `modelReadiness.notReady` with its reason.
- **Step 4, rate limit and exhaustion — PASS.** One attempt seeded 10 min old,
  kill file deleted:
  `SELF-HEAL waiting: the Triton model load is wedged (models LOADING: model-selfheal-probe for 33m32s (suspected culprit)); the last self-heal restart was at 2026-10-17T01:32:00Z and self-heal restarts at most once per 30m; next possible restart at 2026-10-17T02:02:00Z`,
  `selfHeal.state` `cooldown`, no restart. Three attempts seeded within 24 h:
  `SELF-HEAL EXHAUSTED: the Triton model load is wedged (models LOADING: model-selfheal-probe for 35m34s (suspected culprit)) but self-heal already restarted the backend 3 times in the last 24h; not restarting. ... self-heal re-arms at 2026-10-17T22:44:08Z. ...`,
  `selfHeal.state` `exhausted`, no restart.
- **Step 5** (deferral by a continuous workflow): not run, by the owner's
  choice; covered by the unit tests (`test_self_heal_policy.py`).
- **Step 6, clean up — PASS.** With the kill file present, a manual backend
  restart (start line `self-heal off (file /aws_dda/dda_triton/model_status/self-heal.disabled)`),
  the probe and the F3 component removed by the guarded cleanup (the F3 record
  with them), `self_heal.json` and the kill file deleted: `enabled: true`,
  `disabledBy: null`, `attemptsIn24h: 0`, nothing `LOADING`, no restart.
- **Activity veto on real hardware:** no model with a TensorRT engine build is
  deployed on the device (ONNX with CUDA only); `ACTIVITY_CPU_THRESHOLD_S` and
  the GPU veto are verified by host tests only.

## F6 (Portal deployment readiness warning)

Verified by host tests only (FEAT-005's suites). The Portal was not deployed
(owner gate G2 declined: this branch's Portal could replace newer live
code); the live check is deferred to the trunk merge.

## Restore and sustained health

The PPE demo was stopped before the first change and restarted at the end.
Within 2 min it had processed 195 frames, with 23 violation hits and 4
violation clips; the backend stayed healthy for 15 min
while the demo ran. Differences from the pre-state:

- LocalServer 1.0.54 -> 1.0.56 (intended).
- The device's latest deployment carries a LocalServer `reset` of
  `/ModelLoadSelfHealEnabled` next to its unchanged `merge`; the effective
  value is the recipe default `"true"`.
- `aws.greengrass.Cli` 2.18.3 -> 2.18.4: the cloud deployments re-resolved the
  CLI's version range. 2.18.3 is no longer on the device, so this is not
  reversible here.
- After the last R3 capture (2026-10-17, no restart allowed), the backend
  lists `model-r3-probe` as `UNAVAILABLE` in `/feature-configurations` until
  its next restart; its repository entries are gone.

## Other targets (task 14)

Every build below is built from 7f690fc3, the committed branch head. The owner
approved publishing and deploying on each device (gate G3). On each device the
deployment was a copy of that device's own thing deployment with only the
LocalServer version changed. Afterwards each device was rolled back to its
pre-state LocalServer version, because another branch's fixes ship in other
versions and the owner chooses what the devices run until the trunk merge.
Publishing was additive only.

### Builds and published versions

| Target | Build | Gate on the built image | Published | Images (backend / frontend) |
|---|---|---|---|---|
| JP6 (`arm64JP6`) | Built on the arm64 build host. The edgemlsdk layer ran the python_backend clone, the patch, the source grep and the `libtriton_python.so` sentinel. | Exit 0, all six security gates passed, backend unit tests 63 passed. Stream camera gate not run (see below). | **1.0.80** | `8ab42856` / `c0a37de4` |
| JP7 (`arm64JP7`) | Built on the arm64 build host. Backend and recipe changes only; `Dockerfile.jp7` is unchanged. | Exit 0 on the full gate, stream camera gate included, with the container limited to 8 CPUs. | **1.0.57** (mixed build, see below) | `5d64d2e9` / `c31091ae` |
| x86 (`amd64`, CPU) | Built on the x86 workstation (x86_64, Ubuntu 20.04). The generic Dockerfile ran the python_backend clone, the patch, the source grep and the binary sentinel. The built image's `/opt/tritonserver/backends/python/libtriton_python.so` contains the sentinel string. | Exit 0 on the full gate (8 CPUs). All gates passed; full preservation suite 142 passed, 10 skipped. | **1.0.54** | `1e190d98` / `9dad4df7` |

- All three builds ran with `SKIP_BACKEND_TESTS=1`, and the gate body of
  `build-custom.sh` was replayed on the built image before publishing.
  - On the arm64 host, the full JP6 and JP7 builds both stopped in the
    in-image stream camera gate. JP7 stopped in the H.265 RTMP test, the JP5
    hang. JP6 stopped in `test_software_decoding`.
  - In the JP6 image on this host, PyAV's libx265 H.265 encoding of the probe
    sample hangs at the base commit and at the branch head alike, with 1, 4 or
    8 CPUs. This is not caused by this change. The test fixture encodes both
    codecs, so the JP6 replay leaves out the whole stream camera gate.
- Each cloud recipe has `aws.greengrass.Cli` with `DependencyType: SOFT` and
  `ModelLoadSelfHealEnabled: "true"`.
- For JP6, the image ids were checked equal to the build's ids right before
  the push.
- **JP7 1.0.57 is a mixed build.**
  - Its backend image is this branch's.
  - Its frontend image comes from another branch's build, which ran on the
    same host during the push and re-tagged `react-webapp:latest`.
  - It was deployed only for the R1 check below, which exercises the backend
    only, and the device was rolled back afterwards. Do not deploy it anywhere
    else.
- On x86, `create_aws_triton_deps_pkg.sh` fails to rsync the python backend
  files, because an earlier Dockerfile step has already moved them. This
  predates the branch and is unchanged by it. The backend still ships through
  the Triton tarball, as the sentinel check above shows.

### Jetson AGX Orin (JP6, Triton 2.45.0)

**Pre-state.** LocalServer 1.0.77, and three Triton models READY: cookies-binary,
rf-detr-seg-nano and yolo-test. No vLLM model. Nothing was mid-run.

**Deploy of 1.0.80.** COMPLETED in 5 min.

- The five host scripts equal the source by sha256.
- Watchdog start line: `Triton model-load watchdog started (self-heal on (ModelLoadSelfHealEnabled=true); GPU load signal /sys/devices/platform/bus@0/17000000.gpu/load; stall after 15m, at most 3 restarts per 24h and 30m apart)`.
- All models READY. 15 min sustained health.

**R1: PASS.** Backend restarted, so all models were UNKNOWN. `/start` for
yolo-test, then its cleanup 1 s later. The cleanup logged, in order:

- `a load is in flight (state LOADING); waiting up to 300s ...`
- `load finished (READY); unloading before removing its repository entries`
- `unloaded (state UNAVAILABLE)`
- `Cleaned directory` x3
- `Directory cleanup finished`

It exited 0 after 5 s. There was no `pb_stub` line. The two other models were
READY 42 s after their `/start`. A component restart brought yolo-test back to
READY within 10 s.

**R3: partially met, the same as JP5.**

- With `base_model-r3-probe/8/model.py` removed, the stub was relaunched about
  every 13 s: 19 `pb_stub.cc:2081] Failed to preinitialize Python stub: Python model file not found ...`
  lines in 4 min.
- The probe stayed `LOADING` for the whole 208 s observed.
- A sibling load requested 15 s in was queued (`Enqueuing model ... for loading`)
  but stayed blocked. Its model lock was released after 120 s with
  `its load state stayed UNAVAILABLE without a recorded reason for 120s, so the load is treated as ended`.
- After `model.py` was restored, both models were READY within 10 s. The
  guarded cleanup then removed the scratch entries.

**Rollback to 1.0.77.** COMPLETED. All models READY and 15 min sustained
health. The installed component versions equal the pre-state.

### Jetson AGX Thor (JP7, Triton 2.63.0)

**Pre-state.** LocalServer 1.0.55, eight Triton models READY, and the vLLM
model `qwen3-vl-8b-instruct` READY. Nothing was mid-run.

- The owner's `ppe-detector-v3-static-test` workflow is triggered by MQTT. One
  trigger produced a completed run with helmet, human and vest detections.

**Deploy of 1.0.57.** COMPLETED in 16 min.

- The host scripts equal the source.
- Watchdog start line: `... self-heal on (ModelLoadSelfHealEnabled=true); GPU load signal unavailable; ...`.
- Every model READY, and the vLLM model READY: it was reloaded during the
  deployment and its engine process was running.
- A triggered PPE run completed with 1 helmet, 2 human and 1 vest detections.
- 15 min sustained health.

**R1 (non-regression): PASS.** The cleanup output had the same sequence as on
JP5 and JP6 and exited 0. There was no `pb_stub` line.

- After the backend restart, the vLLM model went `LOADING`, then READY through
  its reload after restart.
- The other seven models were READY within 51 s of their `/start`.
- yolo-test was READY within 5 s of a component restart.
- The expected caveat, the vLLM model staying STAGED/UNKNOWN until its next
  lazy load, was not observed. It was READY after both restarts.

R3 was not run. The Thor uses Triton 2.63.0, and the python_backend patch
applies only to the 2.45.0 builds.

**Rollback to 1.0.55.** COMPLETED.

- On the pre-state build, the backend aborted once during vLLM engine start-up,
  about 80 s after it started:
  `forkable.cc:57] Check failed: !std::exchange(is_forking_, true)`, exit
  code 133. Docker restarted it (restart count 1).
  - The same abort is recorded in the `run-detection-visibility` verification
    notes on an earlier build, so it predates this change.
- After the restart, four models were UNKNOWN. They were started and became
  READY, and the vLLM model was READY.
- A PPE run completed with the same detections.
- 15 min sustained health, with no further restart.
- Difference from the pre-state: `aws.greengrass.Cli` 2.18.3 -> 2.18.4,
  re-resolved by the cloud deployments. This cannot be undone here.

### x86 workstation (amd64 CPU, Triton 2.45.0)

**Pre-state.** LocalServer 1.0.50 and four Triton models READY. No workflow
registrations.

**Deploy of 1.0.54.** COMPLETED.

- The host scripts equal the source.
- Watchdog start line: `... self-heal on (ModelLoadSelfHealEnabled=true); GPU load signal unavailable; ...`.
- All models READY. 15 min sustained health.

**R1: PASS.** The cleanup output had the same sequence and exited 0. There was
no `pb_stub` line. The other three models were READY 16 s after their
`/start`, and yolo-test was READY within 5 s of a component restart.

**R3: partially met, the same as JP5.**

- 21 `Failed to preinitialize Python stub` lines in about 3.5 min, one about
  every 10 s.
- The probe stayed `LOADING`, and the sibling load stayed blocked.
- After `model.py` was restored, both models were READY within 10 s. The
  guarded cleanup removed the scratch entries.

**Rollback to 1.0.50.** COMPLETED. All models READY and 15 min sustained health.
The installed component versions and the running backend image equal the
pre-state.

### SSH host keys

The lab host keys were pinned on first use and are recorded outside the
repository. All connections used `StrictHostKeyChecking=yes`.

## Findings

1. **Opt-out command (fixed in bugfix.md Operator notes).** The local
   `greengrass-cli deployment create --update-config '{... "MERGE": {"ModelLoadSelfHealEnabled": "false"}}'`
   reports SUCCEEDED but does not change the configuration of a LocalServer
   that a cloud deployment owns. The cloud configuration merge is the
   supported opt-out.
2. **Disabled WARNING logged once per episode (NIT).** In step 3 no new "self-heal is off" WARNING named the kill file,
   because the watchdog logs that WARNING once per wedge episode and kind, and
   step 2 had already logged it with the configuration reason. The surfaces
   show `disabledBy: "file"`, and the recovery action names the file. Carried
   as a NIT in `triton-python-stub-relaunch-loop`.
3. **`aws.greengrass.Cli` 2.18.3 -> 2.18.4** during the verification's cloud
   deployments (see Restore). The same happened on the Jetson AGX Thor.
4. **The stub relaunch loop (criterion 2.4) also occurs on JP6 and x86.** Both
   reproduce the JP5 behaviour: the stub start is bounded, but it is relaunched
   about every 10-13 s inside one load call, and sibling loads stay blocked.
   This is carried by `triton-python-stub-relaunch-loop`.
5. **Shared image tags can mix builds (follow-up `build-publish-shared-tag-race`).**
   The publish scripts push whatever `flask-app:latest` and
   `react-webapp:latest` point to, without checking the image id. When two
   builds run on one host, a publish can therefore pair one build's backend
   with another build's frontend, as happened with JP7 1.0.57. Recommendation:
   publish by image id, or tag each build's images uniquely.
6. **PyAV libx265 hang in the JP6 image.** The in-image stream camera gate
   cannot complete on the arm64 build host, at the base commit as well. This is
   a follow-up together with the JP5 stream-gate hang.
7. **Native abort during vLLM start-up on the Thor's pre-state build.** The
   absl `forkable.cc` check failed once, and Docker restarted the container.
   This predates this change; see the `run-detection-visibility` notes.

## Not verified

- Self-heal and F3 on JP6, JP7 and x86 (verified on JP5 only; not part of
  task 14).
- The amd64Nvidia target (not built; its build is on hold for an unrelated
  reason).
- Opt-out persistence across later cloud deployments that do not carry the
  merge.
- SIGTERM reaching the convertor's Python process during a Startup.
- The Startup record's `pbStubMissingFile` capture (D5) against a real
  `pb_stub` line (host tests only). The F3 probe blocked in `initialize()`, so
  the field stayed null, and the R3 scratch entry was not loaded by a component
  Startup.
- The x86 stub-CPU-only activity veto.
