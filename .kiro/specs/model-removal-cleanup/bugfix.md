# Bugfix Requirements Document

## Introduction

Removing a vLLM model from a device's Greengrass deployment does not remove the
model from the device. The Greengrass layer honours the removal — the deployment
document no longer lists the component — but the model's staged repository
directory under `/aws_dda/dda_triton/vllm_model_repo/` survives, and everything
downstream of that directory keeps treating the model as present: the
post-restart reconciler (`vllm_runtime/reconciler.py`) counts it as a reload
candidate on every backend container start, and the device model list
(`utils/feature_configs_utils.get_features_vllm`, fed by
`VllmRuntimeManager.list_models`) keeps reporting it.

Two harms follow. First, a **stale status the operator cannot clear through the
product**: the removed model shows FAILED (or, after a manual runtime unload,
STOPPED) in the portal's device model list indefinitely, and there is no
operator-accessible control that removes it. Second, **a removed model delays
every other model's load**: the reconciler loads serially in sorted name order
with a 30 s / 120 s / 480 s bounded backoff, so one dead leftover burns its whole
retry budget before the reconciler reaches a model the operator actually wants.

The intent that removal unstages the model already exists in the design: the
vLLM model component recipe emits a Shutdown lifecycle running
`vllm_model_prep.py --cleanup --model_name … --component_name …`, and that
`cleanup()` path unloads the model and `rmtree`s the staged directory. In this
incident it did not produce a clean removal. **Why it did not is an open
question** (see *Open Questions*), to be settled with evidence during
exploration rather than assumed. Independently of that answer, the device has no
second line of defence: nothing on the device ever reconciles the set of staged
repositories against the set of currently deployed model components, so a
cleanup that is skipped, refused, interrupted or never invoked leaves state
behind permanently.

### Incident record (jetson-thor1, verified by direct device inspection this session)

Device: Greengrass core `jetson-thor1`, JetPack 7 / `arm64_jp7` (R39, 122 GiB
unified memory), account 164152369890, us-east-1, running
`aws.edgeml.dda.LocalServer.arm64JP7` 1.0.10.

The vLLM model component `model-vllm-qwen3-5-9b-jetson-xavier-jp7` (model name
`qwen3-5-9b`) was removed from the deployment. Revision 22 of
`ssh-tunnel-on-jetson-thor1` correctly no longer lists it.

Staged repositories still on the device after the removal:

```
/aws_dda/dda_triton/vllm_model_repo/qwen3-vl-8b-instruct
/aws_dda/dda_triton/vllm_model_repo/qwen3-5-9b     <- component REMOVED, repo still present
```

The surviving `qwen3-5-9b/` directory contained `1/`, `config.pbtxt` and
`.dda_stage_owner.json`, all root-owned, timestamped 19:51 / 20:44 — i.e. from
staging time, not from a post-removal write.

On every backend container start the reconciler picks it up again:

```
vLLM reconciler started (staged-model reload after restart).
vLLM reconciler: re-driving the load of 2 staged model(s): qwen3-5-9b, qwen3-vl-8b-instruct
vLLM reconciler: requesting load of 'qwen3-5-9b' (attempt 1/4)
... load fails (this model genuinely cannot load on this image) ...
vLLM reconciler: load of 'qwen3-5-9b' failed; retrying in 30 seconds (3 attempt(s) left)
... 120 s ... 480 s ...
vLLM reconciler: model 'qwen3-5-9b' FAILED to reload after 4 attempts — automatic
    retries are exhausted; retained reason: … The model stays FAILED until an
    explicit load or a component restart re-drives it.
vLLM reconciler: requesting load of 'qwen3-vl-8b-instruct' (attempt 1/4)   <- ~11 min later
```

Measured delay imposed on the model that was still deployed: 21:00:34Z ->
21:11:17Z (~11 minutes) before its first load request was issued. On this device
that was the difference between a workflow having a VLM and not having one.

The only route found to clear the stale entry was calling the LocalServer
runtime API on the device (`POST /v2/repository/models/{name}/unload`, which
writes the `.dda_explicit_unload` tombstone), reached over an IoT Secure Tunnel
plus SSH. That is not an operator-accessible path, and it does not remove the
staged directory either — it converts the lingering entry from FAILED to
UNLOADED/STOPPED.

Verified in code: the device's only model start/stop controls
(`GET /feature-configurations/models/{modelName}/start|stop`) route exclusively
to the Triton and LFV paths and reject any name not prefixed `model-`, so vLLM
model names such as `qwen3-5-9b` cannot reach them at all.

### Out of scope

- The `qwen3_5` transformers/vLLM version gap that made that particular model
  unloadable (transformers 4.57.6 does not recognise model type `qwen3_5`; vLLM
  0.11.3.dev0 pins transformers<5). Separate, already-diagnosed issue.
- The LocalServer container restart loop separately under investigation on this
  device.

## Bug Analysis

### Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type DeviceState
    X.staged     : set of model names having a staged repository under VLLM_MODEL_DIR
    X.tracked    : map model name -> ModelStatus held by the runtime manager
    X.deployed   : set of model names whose vLLM model component is present in the
                   device's CURRENT Greengrass deployment
  OUTPUT: boolean

  // On-device state exists for a model whose component is no longer deployed
  RETURN EXISTS m IN (X.staged UNION keys(X.tracked)) WHERE m NOT IN X.deployed
END FUNCTION
```

Counterexample from the incident: `X.staged = {qwen3-5-9b, qwen3-vl-8b-instruct}`,
`X.deployed = {qwen3-vl-8b-instruct}` (revision 22) -> `qwen3-5-9b` satisfies the
condition.

### Current Behavior (Defect)

1.1 WHEN a vLLM model component is removed from a device's Greengrass deployment
THEN the model's staged repository `{VLLM_MODEL_DIR}/{model_name}/` survives on
the device, so the removal takes effect at the Greengrass layer only

1.2 WHEN the LocalServer backend container starts with such a leftover staged
repository THEN the reconciler counts the removed model as a reload candidate and
re-drives its load with the full 4-attempt / 30-120-480 s budget, spending device
memory and time on a model the operator removed

1.3 WHEN a removed model's load fails THEN it reports FAILED indefinitely in the
device model list, and the operator has no product control that clears it — the
only start/stop controls route to Triton/LFV and reject non-`model-` names, so a
vLLM model name never reaches them

1.4 WHEN a leftover model sorts before a still-deployed model in the reconciler's
serial, sorted scan THEN the still-deployed model's first load request is delayed
by the leftover's entire failed backoff budget (measured ~11 minutes,
21:00:34Z -> 21:11:17Z)

1.5 WHEN an operator does reach the device runtime and issues an explicit unload
for the removed model THEN the staged directory stays on disk and the model
merely changes from FAILED to UNLOADED/STOPPED in the reported list — a stale
entry still lingers, and the tombstone is the only thing keeping the reconciler
off it

1.6 WHEN the component Shutdown `--cleanup` does not remove the staged
repository — for any reason: never invoked, refused, interrupted, or failing
mid-way — THEN nothing else on the device ever removes it: there is no sweep, no
startup reconciliation of staged repositories against currently deployed
components, and no expiry, so the leftover is permanent until a human deletes it
by hand

1.7 WHEN a cleanup is skipped or refused THEN the fact is at most a WARNING in
the removed component's own log, which is exactly the log an operator no longer
looks at after removal, so a silent no-op cleanup is not observable through any
status surface

### Expected Behavior (Correct)

2.1 WHEN a vLLM model component is removed from a device's Greengrass deployment
THEN the system SHALL remove that model's on-device state completely: the staged
repository directory (with the tombstone and owner marker it contains), any
leftover staging temp siblings for that model, and the runtime manager's
in-memory tracking entry

2.2 WHEN the backend container starts THEN the reconciler SHALL NOT re-drive a
load for a model whose component is no longer deployed — a removed model SHALL
contribute zero load attempts and zero backoff

2.3 WHEN a model's on-device state has been removed THEN the model SHALL
disappear from the reported device model list within the existing status
propagation bound, in no state at all — not FAILED, not STOPPED/UNLOADED, not
LOADING

2.4 WHEN a removed model's cleanup did not complete at component-Shutdown time
THEN the system SHALL still converge without operator intervention: a subsequent
backend start or deployment SHALL complete the removal, so complete removal SHALL
NOT depend on a single lifecycle-script execution succeeding

2.5 WHEN a model has been removed THEN the loads of the models that are still
deployed SHALL NOT be delayed by it: the reconciler's first load request for a
still-deployed model SHALL NOT wait on any removed model's retry schedule

2.6 WHEN an operator removes a model THEN removal SHALL be observable through the
product's normal surfaces (the device model list stops listing the model),
without requiring an IoT Secure Tunnel, SSH, or a direct call to the device
runtime API

2.7 WHEN a model's on-device state is removed THEN the shared Hugging Face
weights cache (`HF_HOME=/aws_dda/hf_cache`) SHALL be handled by a deliberate,
recorded decision: the system SHALL NOT delete cached weights that any other
staged or deployed model still references, and the retain-or-delete choice SHALL
be stated with its rationale rather than left implicit

2.8 WHEN cleanup runs for a model THEN its outcome SHALL be observable — removal
performed, removal skipped with the reason (for example an ownership refusal), or
removal failed with the error — so a cleanup that silently does nothing cannot
recur undetected

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a vLLM model component is still deployed and the LocalServer backend
restarts THEN the system SHALL CONTINUE TO re-drive that model's load through the
reconciler (spec `vllm-model-reload-after-backend-restart`, Requirements 2.1,
2.2); removal cleanup SHALL NEVER unstage a model whose component is still
deployed

3.2 WHEN an operator issues an explicit unload for a still-deployed model
(`POST /v2/repository/models/{name}/unload`) THEN the system SHALL CONTINUE TO
shut the engine down, reclaim memory, clear the starvation latch, write the
`.dda_explicit_unload` tombstone, keep the staged repository on disk, and report
UNLOADED/STOPPED — an explicit unload SHALL NOT become a delete

3.3 WHEN a still-deployed, tombstoned model is present at backend start THEN the
reconciler SHALL CONTINUE TO skip it (Requirements 2.4, 3.5 of
`vllm-model-reload-after-backend-restart`)

3.4 WHEN an explicit load arrives for a tombstoned, staged model THEN the system
SHALL CONTINUE TO clear the tombstone first and load, re-arming reconciliation

3.5 WHEN a model component deploys or re-deploys THEN Startup SHALL CONTINUE TO
stage atomically (copy to a temp sibling, then `rmtree` + `rename`), which clears
any prior tombstone by construction, and SHALL CONTINUE TO record the owner
marker under the per-model stage lock

3.6 WHEN a component that is not the recorded owner runs `--cleanup` for a shared
`--model_name` THEN the system SHALL CONTINUE TO refuse both the unload and the
removal and still exit 0 (spec `jp6-vllm-kv-cache-oom-regression`, H11/H12: a
non-owning teardown destroyed another component's freshly-loaded model 0.6 s
after its load succeeded); the removal fix SHALL NOT weaken this guard into
deleting another component's model

3.7 WHEN a cleanup or removal step hits a filesystem error, a stuck advisory
lock, or an unreadable/malformed owner marker THEN the system SHALL CONTINUE TO
fail open — log and proceed, exit 0 — never failing a Greengrass deployment and
never bricking the device

3.8 WHEN a model is in a transient state but still deployed — staged with its
load not yet requested, LOADING, or FAILED with retries outstanding — THEN the
system SHALL CONTINUE TO retain its staged repository; a transient state SHALL
NOT be read as "removed" and SHALL NOT cause data loss

3.9 WHEN vision (Triton) models or LFV models are deployed, stopped or removed
THEN the system SHALL CONTINUE TO behave exactly as today; nothing here touches
`triton_model_repo`, the embedded Triton, or the LFV path

3.10 WHEN models that are still deployed are reconciled THEN the existing serial,
sorted-order reconciliation with its bounded 4-attempt retry budget and the
validated KV-OOM unload -> reload recovery SHALL CONTINUE TO apply unchanged

### Properties

```pascal
// Property: Fix Checking — complete removal
FOR ALL X WHERE isBugCondition(X) DO
  orphans ← { m IN (X.staged UNION keys(X.tracked)) : m NOT IN X.deployed }
  X'      ← removalCleanup'(X)
  FOR ALL m IN orphans DO
    ASSERT NOT repositoryExists(X', m)
    ASSERT m NOT IN keys(reportedModels(X'))
    ASSERT loadAttempts(reconcile'(X'), m) = 0
  END FOR
END FOR
```

```pascal
// Property: Fix Checking — a removed model delays nothing
FOR ALL X WHERE isBugCondition(X) DO
  R ← reconcile'(removalCleanup'(X))
  FOR ALL d IN X.deployed WHERE d IN X.staged DO
    ASSERT firstLoadRequestDelay(R, d) = delayFromDeployedModelsOnly(R, d)
  END FOR
END FOR
```

```pascal
// Property: Preservation Checking — non-buggy device states are untouched
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

```pascal
// Property: Preservation Checking — still-deployed models are untouched even
// when the device state DOES satisfy the bug condition
FOR ALL X WHERE isBugCondition(X) DO
  X' ← removalCleanup'(X)
  FOR ALL d IN X.deployed DO
    ASSERT repositoryExists(X', d)      = repositoryExists(X, d)
    ASSERT tombstonePresent(X', d)      = tombstonePresent(X, d)
    ASSERT ownerMarker(X', d)           = ownerMarker(X, d)
    ASSERT reportedStatus(X', d)        = reportedStatus(X, d)
  END FOR
END FOR
```

Where **F** is the current (unfixed) device behavior and **F'** the fixed
behavior.

### Open Questions (to be settled by exploration during the fix, not assumed)

The Shutdown lifecycle is supposed to unstage the model
(`python3 /aws_dda/vllm_model_prep.py --cleanup --model_name {model} --component_name {component}`).
Why it did not produce a clean removal in this incident was **not established**.
Each candidate below must be confirmed or refuted with evidence:

- **Q1** Does Greengrass run the Shutdown lifecycle at all when a component is
  *dropped from a revised deployment*, as opposed to stopped or restarted in
  place? Evidence needed: Greengrass component/deployment logs for revision 22 on
  `jetson-thor1` showing whether a Shutdown script ran for the removed component.
- **Q2** Did `--cleanup` run but get **refused by the ownership guard**?
  `cleanup()` skips both the unload and the `rmtree` — and still exits 0 — when
  the staged repository's `.dda_stage_owner.json` records a `component_name`
  different from the `--component_name` passed. The surviving directory *did*
  contain that marker. Evidence needed: the marker's recorded `component_name`
  compared against `model-vllm-qwen3-5-9b-jetson-xavier-jp7`, plus the
  "REFUSING --cleanup" WARNING in the component log. Note that per-target
  component renaming exists (spec `vllm-multi-arch-publish-conflict`), so a
  marker written under a different component name is plausible but unverified.
- **Q3** Did `--cleanup` run and reach the removal, but fail or get killed?
  `cleanup()` holds the per-model stage lock across a `POST /unload` with a 300 s
  timeout, inside a Shutdown whose recipe timeout is 900 s; a concurrently
  restarting LocalServer container could stall the unload long enough for the
  Shutdown to be killed before the `rmtree`.
- **Q4** Did removal **ordering** matter — component removed before vs after the
  LocalServer restart, or a Startup re-staging the repository after a successful
  cleanup? The surviving directory's mtimes (19:51 / 20:44) both predate the
  removal; whether either corresponds to a re-stage is to be established.
- **Q5** What device-side signal authoritatively answers "which vLLM model
  components are currently deployed?" (Greengrass deployment document, component
  list over IPC, or something else) — this is what any convergence mechanism for
  2.4 must read, and it must be available to the backend without cloud calls.
- **Q6** What is the right treatment of the Hugging Face weights cache
  (2.7): the cache is shared across models and reused by design (it exists to
  avoid re-downloads on container recreation), so removal must not delete weights
  another model references. Whether a removed model's *exclusively* held weights
  should be reclaimed at all, and if so on what signal, is open.
