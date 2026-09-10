# Bugfix Requirements Document

## Introduction

The previous revision of this document was built on a premise that has since been
**refuted by evidence** from CloudWatch Logs and the Greengrass APIs. It claimed
that the model component's Shutdown `--cleanup` failed to remove the staged
repository. It did not fail. On `jetson-thor1` that cleanup ran and **fully
succeeded 19 times**, 17 of them after the removal, each in 1.2-15 s.

What actually happened is one level up. The removed model component was still the
target of a **HARD dependency from a workflow component that was still
deployed**, so Greengrass never undeployed it: it demoted the component from the
deployment's root set while keeping it *installed* as a non-root dependency, kept
restarting it on dependency health events, and each restart's Startup re-staged
the repository — 119 ms after the cleanup that had just correctly removed it. The
staged directory was not *left behind*; it was **continuously recreated**. The
removal was a silent no-op at the Greengrass layer, not a partial success at the
device layer.

That root cause is portal-side, and the fix for it belongs to the companion spec
**`.kiro/specs/deployment-preflight-validation/`**, where removing a model while
a selected workflow HARD-depends on it must be refused (or the dependency
surfaced) instead of silently accepted. **This spec is scoped to the device side
only**, to the two concerns that stand on their own merits regardless of the
portal gate:

1. **A device-side convergence sweep.** A *genuinely* orphaned staged repository
   — one whose owning component is absent from the device's installed component
   set — is removed without operator intervention, with a per-repository
   observable outcome. Being honest about its value: **this sweep would not have
   fired in the incident, and that is correct**, because the component was still
   installed and running. Its value is the genuinely-orphaned case: a Shutdown
   that never ran because the Nucleus was killed mid-shutdown, an `rmtree` that
   partially completed, an orphaned `.staging-*` temp sibling.
2. **Reconciler cost containment.** In the incident the reconciler was *right* to
   keep retrying a still-deployed model. The harm was structural: the serial,
   sorted scan (`src/backend/vllm_runtime/reconciler.py:170` `_candidates`) lets
   one unloadable model spend its entire 30 / 120 / 480 s budget *ahead of* a
   healthy one. This is an ordering / parallelism / fairness defect, entirely
   independent of removal.

### The one definition that makes this fix safe

Everything here turns on how "still deployed" is defined on the device. The bug
condition's `X.deployed` is **pinned to the resolved INSTALLED component set
reported by Greengrass IPC `ListComponents`** — root components *and* non-root
dependencies, in any lifecycle state — and explicitly **not** to the deployment
document's / `effectiveConfig.yaml`'s root component set.

This is not a stylistic preference. The incident is the proof: the root component
set is exactly the set that **omitted `model-vllm-qwen3-5-9b-jetson-xavier-jp7`
while a deployed workflow still required it**. A sweep keyed on root components
would have judged the live, running, workflow-backing model to be an orphan and
deleted it — causing the very outage this spec exists to prevent, and violating
this spec's own Requirement 3.1. See *Bug Condition* for the side-by-side.

### Incident record (jetson-thor1) — corrected

Device: Greengrass core `jetson-thor1`, JetPack 7 / `arm64_jp7` (R39, 122 GiB
unified memory), account 164152369890, us-east-1. Model component
`model-vllm-qwen3-5-9b-jetson-xavier-jp7`, model name `qwen3-5-9b`.

**The removal was revision 21, not 22.** Revision 21 of
`ssh-tunnel-on-jetson-thor1` at 2026-08-26T20:39:36.933Z dropped the model
component. Revision 22 followed 2.3 s later at 20:39:39.201Z, re-adding a
different workflow. The device dequeued **revision 22** at 20:43:40.648Z, because
revision 21 had already been replaced in the queue before it was executed.

**The Shutdown ran, and it worked.** The published recipe did carry it:

```json
"Shutdown": {
  "Script": "python3 /aws_dda/vllm_model_prep.py --cleanup --model_name qwen3-5-9b --component_name model-vllm-qwen3-5-9b-jetson-xavier-jp7",
  "Timeout": 900, "requiresPrivilege": true, "runWith": {"posixUser": "root"}
}
```

Every one of 19 cycles logged `Cleaned directory:
/aws_dda/dda_triton/vllm_model_repo/qwen3-5-9b` followed by `Directory cleanup
finished`. Nothing came close to a timeout: the unload wall time was 5.9 s
against `UNLOAD_REQUEST_TIMEOUT_SECONDS = 300`
(`src/backend/dda_triton/vllm_model_prep.py:204`) and the whole Shutdown took
8.9 s against the recipe's `Timeout: 900`
(`edge-cv-portal/backend/functions/greengrass_publish.py:675`).

**The repository survived because the component was reinstated, then re-staged.**
The still-deployed workflow `dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8`
v12.0.0 declares:

```json
"ComponentDependencies": {
  "model-vllm-qwen3-5-9b-jetson-xavier-jp7": {"VersionRequirement": ">=0.0.0", "DependencyType": "HARD"},
  "aws.edgeml.dda.LocalServer.arm64JP7":     {"VersionRequirement": ">=1.0.0",  "DependencyType": "HARD"}
}
```

emitted by design by `workflow_packaging.model_component_dependencies`
(`edge-cv-portal/backend/functions/workflow_packaging.py:1576`; the `>=0.0.0`
HARD at `:1617`). Greengrass therefore kept the component installed as a non-root
dependency and restarted it. The re-stage landed **119 ms** after the successful
cleanup:

```
20:44:05.299Z INFO:Cleaned directory: /aws_dda/dda_triton/vllm_model_repo/qwen3-5-9b
20:44:05.320Z GenericExternalService: service-set-state {STOPPING -> INSTALLED} ; {INSTALLED -> STARTING}
20:44:05.418Z INFO:Staged vLLM model 'qwen3-5-9b' at '/aws_dda/dda_triton/vllm_model_repo/qwen3-5-9b'
```

The restart trigger was a dependency health event, never a removal:

```
20:43:56.223Z GenericExternalService: service-restart. Restarting service because
              dependency aws.edgeml.dda.LocalServer.arm64JP7 was in a bad state.
```

**Greengrass's own bookkeeping shows the split.** After the removal revision
completed, `GroupToRootComponents` omits `qwen3-5-9b` while `ComponentToGroups`
still contains it. That state persisted across revisions **22-52** — roughly 30
deployments, LocalServer 1.0.10 -> 1.0.19.

**The component left the device only when the depending workflow did.** On
2026-09-01T16:18:30.543Z, revision 53:

```
16:18:30.543Z DeploymentConfigMerger: merge-config. Removing services.
              {service-to-remove=[model-vllm-qwen3-5-9b-jetson-xavier-jp7, dda.workflow.421f8233-...]}
16:18:31.727Z INFO:Cleaned directory: /aws_dda/dda_triton/vllm_model_repo/qwen3-5-9b
16:18:33.413Z ComponentStore: delete-component-finish
```

with **no re-stage after**. That was the only one of 19 cleanups that stuck — and
it stuck for the obvious reason: nothing depended on the component any more.

**The surviving directory's mtimes were misread.** The `20:44` timestamp is the
**post-removal re-stage** (20:44:05.418Z), not staging time. Only `19:51` is the
original stage. The previous revision's claim that "the surviving directory's
mtimes (19:51 / 20:44) both predate the removal" is wrong, and it is what made
the failed-cleanup premise look plausible.

### Corrections to the previous revision of this document

| Previous clause | Verdict |
|---|---|
| 1.1 removal "takes effect at the Greengrass layer only" | **REFUTED.** The removal did not take effect at the Greengrass layer at all. Greengrass kept the component installed as a non-root dependency and kept restarting it. |
| 1.6 cleanup "does not remove the staged repository … nothing else ever removes it" | **REFUTED as diagnosis** — cleanup removed it 19 times. **Survives as a genuine gap**, restated as 1.3 below: removal is not *stable* while the component is reinstated, and there is no sweep for the genuinely-orphaned case. |
| 1.2 reconciler re-drives a removed model | **Survives, reframed** (1.4). The model was still deployed, so the retries were correct; the defect is who pays for them. |
| 1.3 no product control clears a FAILED vLLM entry | **Survives** (1.6), verified in code. Note the entry was *truthful* in the incident. |
| 1.4 `~11 min` delay, 21:00:34Z -> 21:11:17Z | **Structure survives, figure is cloud-unverifiable** (1.5). The reconciler's log never reaches CloudWatch. |
| 1.5 explicit unload leaves the directory and only flips FAILED -> UNLOADED | **Survives unchanged** (1.7). |
| 1.7 a skipped cleanup is at most a WARNING in a log nobody reads | **Partly refuted** (1.8). The component's own log *is* in CloudWatch and legible — it is what allowed this diagnosis. What never reaches the cloud is the reconciler's log and the vLLM staged-model set. |
| 2.6 removal observable "through the product's normal surfaces without a tunnel" | **Reclassified.** This is a **new propagation requirement**, not a consequence of fixing cleanup. Kept as 2.6 and flagged as separate, additional scope. |
| 2.7 HF cache treatment "to be decided" | **Already decided in-repo.** 2.7 now *cites* `docs/multi-runtime-inference.md` §22.1 instead of re-deciding it. |
| Q2 ownership-guard refusal / `vllm-multi-arch-publish-conflict` renaming | **REFUTED.** Zero `REFUSING` hits across all six `model-vllm-*` log groups. |

### Out of scope

- **The incident's root cause itself** — the portal accepting a model removal
  while a selected workflow HARD-depends on that model. Folded into
  `.kiro/specs/deployment-preflight-validation/`. Nothing in this spec fixes the
  incident; this spec fixes two device-side weaknesses the incident exposed.
- **The `qwen3_5` transformers/vLLM version gap** that made that particular model
  unloadable (transformers 4.57.6 does not recognise model type `qwen3_5`; vLLM
  0.11.3.dev0 pins transformers<5). Separately diagnosed.
- **The LocalServer container restart loop** that produced the dependency
  bad-state restarts. Separately under investigation; it is the *trigger* of the
  re-stages, not the reason removal was unstable.
- **Modifying the Shutdown `cleanup()` path.** Proven correct 19/19; see 3.11.
- **HF weights cache eviction.** Already decided against as an automatic side
  effect (`docs/multi-runtime-inference.md` §22.1); see 2.7 and 3.13.
- **Triton (`triton_model_repo`), the embedded Triton, and the LFV path.**

## Bug Analysis

### Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type DeviceState
    X.staged     : set of model names having a staged repository under VLLM_MODEL_DIR
    X.temps      : set of leftover '.staging-{model}-*' siblings under VLLM_MODEL_DIR
    X.owner(m)   : component_name recorded in {VLLM_MODEL_DIR}/{m}/.dda_stage_owner.json,
                   or NONE when the marker is absent, unreadable or malformed
    X.installed  : set of component names reported by Greengrass IPC ListComponents
                   -- the RESOLVED INSTALLED set: ROOT components AND NON-ROOT
                   dependencies, in ANY lifecycle state (NEW, INSTALLED, STARTING,
                   RUNNING, STOPPING, BROKEN, FINISHED)
    X.candidates : reconciler reload candidates, sorted by name (reconciler._candidates)
  OUTPUT: boolean

  RETURN isOrphanedStage(X) OR isHeadOfLineBlocked(X)
END FUNCTION

FUNCTION isOrphanedStage(X)
  // Concern 1: on-disk state whose owning component is genuinely gone
  RETURN EXISTS m IN (X.staged UNION X.temps) WHERE
             X.owner(m) IS NOT NONE          // fail open on a missing/bad marker
         AND X.owner(m) NOT IN X.installed   // PINNED: ListComponents, not root set
END FUNCTION

FUNCTION isHeadOfLineBlocked(X)
  // Concern 2: one candidate's retry budget charged to another candidate
  RETURN EXISTS a, b IN X.candidates WHERE
             a < b                                  // 'a' sorts first
         AND willExhaustRetries(a)
         AND firstLoadRequestDelay(b) >= backoffBudget(a)
END FUNCTION
```

#### Why `X.deployed` is pinned to `ListComponents` and NOT to root components

**This is the single most important correction in this rewrite.** The two
readings disagree precisely on the incident, and one of them is destructive.

| Reading | Verdict on the incident | Consequence |
|---|---|---|
| **REJECTED** — deployment document / `effectiveConfig.yaml` ROOT components | `isBugCondition(X)` is **TRUE**: `GroupToRootComponents` omitted the model from revision 21 onwards | The sweep deletes `qwen3-5-9b`, a model that was **installed, RUNNING, and HARD-required by a deployed workflow**. This violates Requirement 3.1 and manufactures the outage the spec is meant to prevent. |
| **PINNED** — IPC `ListComponents` resolved INSTALLED set | `isBugCondition(X)` is **FALSE** throughout revisions 22-52: `ComponentToGroups` still contained the component and `ListComponents` reported it installed | The sweep correctly does nothing. It becomes eligible only after revision 53 at 16:18:30.543Z — by which point the Shutdown cleanup had already removed the directory at 16:18:31.727Z, so the sweep finds nothing to do. Correct in both windows. |

The backend *can* read the deployment document — `src/backend/utils/utils.py:56`
already parses it, `/aws_dda` is bind-mounted (`src/docker-compose.yaml:59`) and
`KERNEL_ROOT_PATH` is passed (`recipe-arm64-jp7.yaml:96`) — which is exactly why
the rejection has to be written down rather than left to inference.

The pinned signal is already available with no new plumbing:
`recipe-arm64-jp7.yaml:63-65` grants `aws.greengrass#ListComponents` and
`aws.greengrass#GetComponentDetails` on `resources: ['*']`, and
`src/backend/utils/gg_utils.py:145 list_all_gg_components_with_details()` already
returns every component with version and state over the process-wide IPC client.

`.dda_stage_owner.json`'s `component_name` is the **only device-side artifact
that maps a model name to a component name**, so it stays as the join key between
a staged repository and `ListComponents`. When it is missing or malformed the
sweep **retains** the repository, matching the existing fail-open contract of
`read_owner_marker` / `marker_owner`
(`src/backend/dda_triton/vllm_model_prep.py:531`, `:564`).

#### Counterexamples

Genuinely orphaned (what the sweep is for):

- A Shutdown `--cleanup` that never ran because the Nucleus or the host was
  killed mid-shutdown, leaving `{VLLM_MODEL_DIR}/{m}/` with its marker naming a
  component that `ListComponents` no longer reports.
- A partially completed `rmtree`: the marker or `1/` survives, the component is
  gone.
- An orphaned `.staging-{m}-*` temp sibling from an interrupted atomic stage,
  with no corresponding installed component.

Head-of-line blocking (verified in code, independent of removal): two staged
models, `a` unloadable and sorting first, `b` healthy — `b`'s first load request
waits for `a`'s full 30 + 120 + 480 s schedule.

**Not a counterexample** (and this is the point): the incident itself. Under the
pinned definition `isOrphanedStage` is false for `qwen3-5-9b` at every moment
between revisions 22 and 52.

### Current Behavior (Defect)

1.1 WHEN a vLLM model component is dropped from a deployment's root component set
while another still-deployed component declares a HARD dependency on it THEN
Greengrass keeps the component installed as a non-root dependency, restarts it on
dependency health events, and its Startup re-stages the repository — so the
removal is a silent no-op on the device and the staged repository is
**recreated**, not left behind (`20:44:05.299Z Cleaned directory` ->
`20:44:05.418Z Staged vLLM model`, 119 ms; `GroupToRootComponents` omits the
component while `ComponentToGroups` retains it, revisions 22-52). *Root cause;
fixed in `deployment-preflight-validation`, not here.*

1.2 WHEN the model component's Shutdown `--cleanup` runs THEN it completes
correctly — 19/19 executions on this device, 17 of them after the removal
revision, 1.2-15 s each, unload 5.9 s against a 300 s timeout and Shutdown 8.9 s
against a 900 s recipe timeout — so removal of on-device state is **not** the
defect; the defect is that removal is not **stable** while the component keeps
being reinstated

1.3 WHEN a staged repository's owning component is genuinely absent from the
device's installed component set — a Shutdown that never ran because the Nucleus
was killed mid-shutdown, an `rmtree` that partially completed, an orphaned
`.staging-*` temp sibling — THEN nothing on the device ever removes it: there is
no sweep, no reconciliation of staged repositories against the installed
component set at backend start or at deployment, and no expiry, so the leftover
is permanent until a human deletes it by hand

1.4 WHEN the LocalServer backend container starts with a staged repository whose
model cannot load THEN the reconciler re-drives that load with the full 4-attempt
/ 30-120-480 s budget, serially, in sorted name order
(`reconciler.py:170 _candidates`) — correct in itself for a still-deployed model,
but the entire cost is charged to whatever sorts behind it

1.5 WHEN an unloadable model sorts before a healthy one THEN the healthy model's
first load request is delayed by the unloadable one's whole backoff budget. The
structure is verified in code. The observed magnitude — ~11 minutes, 21:00:34Z ->
21:11:17Z — is **NOT verifiable from the cloud** and rests on the original
on-device observation only: the reconciler's log never reaches CloudWatch
(`/aws/greengrass/UserComponent/us-east-1/aws.edgeml.dda.LocalServer.arm64JP7`
carries only the docker-compose wrapper's stdout, and `filter_log_events` for
`"qwen3-5-9b"` across that whole group returns 0 hits)

1.6 WHEN a staged vLLM model reports FAILED THEN the operator has no product
control that clears it: the device's only model start/stop controls
(`GET /feature-configurations/models/{modelName}/start|stop`) route exclusively
to the Triton and LFV paths and reject any name not prefixed `model-`, so a vLLM
model name such as `qwen3-5-9b` cannot reach them at all (verified in code). In
the incident that FAILED entry was **truthful** — the model really was still
deployed — but it was still unclearable

1.7 WHEN an operator reaches the device runtime and issues an explicit unload for
such a model THEN the staged directory stays on disk and the model merely changes
from FAILED to UNLOADED/STOPPED in the reported list — a stale entry still
lingers, and the `.dda_explicit_unload` tombstone is the only thing keeping the
reconciler off it. Reaching that endpoint required an IoT Secure Tunnel plus SSH

1.8 WHEN the reconciler spends a model's retry budget, or a sweep would skip a
repository, THEN none of it is observable from the cloud: the reconciler's log is
not shipped, and there is no cloud surface for the vLLM model list at all —
`src/backend/endpoints/feature_config.py:119` filters `entry.type ==
"TritonModel"` before `model_status_shadow.report(...)`, and a live shadow read
shows only ONNX components. The model component's own log **is** in CloudWatch
and legible, which is what made this diagnosis possible

### Expected Behavior (Correct)

2.1 WHEN a staged repository (or a `.staging-*` temp sibling) has an owner marker
naming a component that is absent from the device's installed component set at a
convergence point THEN the system SHALL remove that repository — its contents,
its tombstone, its owner marker and the matching temp siblings — together with
the runtime manager's in-memory tracking entry, without operator intervention, so
that complete removal does not depend on a single lifecycle-script execution
having succeeded

2.2 WHEN the system determines whether a model is still deployed for the purpose
of ANY removal decision THEN it SHALL read the resolved INSTALLED component set
from Greengrass IPC `ListComponents` (root components AND non-root dependencies,
in any lifecycle state) and SHALL NOT use the deployment document's or
`effectiveConfig.yaml`'s root component set — because the root set omitted a
model that a deployed workflow still HARD-required, and a sweep keyed on it would
have deleted a live model in violation of 3.1

2.3 WHEN a staged repository has no owner marker, or one that is unreadable or
malformed THEN the system SHALL retain the repository (fail open), take no
destructive action, and record the reason — matching the existing fail-open
contract of `read_owner_marker` / `marker_owner`

2.4 WHEN the convergence sweep evaluates a staged repository THEN its outcome
SHALL be observable per repository: removal performed, removal skipped with the
reason (owner still installed, marker missing or malformed, transient state), or
removal failed with the error — so a sweep that silently does nothing cannot
recur undetected

2.5 WHEN the reconciler has more than one reload candidate THEN one candidate's
failure and backoff schedule SHALL NOT delay another candidate's first load
request: the reconciler SHALL bound the cost any single candidate can impose on
the others through ordering, concurrency or fairness, independently of whether
any model has been removed

2.6 **(NEW, ADDITIONAL SCOPE — not a consequence of 2.1-2.5.)** WHEN a vLLM
model's on-device state changes THEN the change SHOULD be observable through the
product's normal surfaces without an IoT Secure Tunnel, SSH or a direct call to
the device runtime API. This requires a **new propagation path**: today
`feature_config.py:119` filters `entry.type == "TritonModel"` before reporting to
the shadow, so no vLLM model ever reaches the cloud, and the reconciler's log is
not shipped either. Delivering 2.1-2.5 does not deliver this. It is stated here
so it is not lost, and it MAY be scoped to its own spec

2.7 WHEN a model's on-device state is removed THEN the shared Hugging Face
weights cache SHALL be left untouched, per the decision already recorded in
`docs/multi-runtime-inference.md` §22.1 (lines 664-679): undeployment
deliberately does not remove `$HF_HOME/hub`, so a redeploy avoids the
re-download, and because safe automatic eviction would require cross-component
reference counting — sharing is by HF repo id, not by model name
(`HF_HOME=/aws_dda/hf_cache` on all three services,
`src/docker-compose.yaml:128/:197/:254`; cache folder `models--{org}--{name}`,
`src/backend/vllm_runtime/memory_budget.py:738-744`). §22.2 documents the manual
escape hatch. IF a reclaim path is ever wanted THEN it SHALL be an explicit
operator action that reports reclaimable bytes, and never an automatic side
effect of removal

2.8 WHEN the convergence sweep runs THEN it SHALL be idempotent, bounded in time,
and non-blocking: it SHALL NOT delay the backend's readiness or the runtime's
service of requests, SHALL NOT fail a Greengrass deployment, and a second run
over converged state SHALL touch nothing

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a vLLM model component is present in the device's installed component
set and the LocalServer backend restarts THEN the system SHALL CONTINUE TO
re-drive that model's load through the reconciler (spec
`vllm-model-reload-after-backend-restart`, Requirements 2.1, 2.2); the
convergence sweep SHALL NEVER unstage a model whose owning component is still
installed — **including a component that is installed only as a NON-ROOT
dependency**, which is exactly the incident's configuration

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
after its load succeeded). This guard was **never implicated in this incident** —
`filterPattern='"REFUSING"'` across all six `model-vllm-*` log groups returns 0
hits, and both sides of the comparison are the same per-target value by
construction (`greengrass_publish.py:1036/:1049` pass one
`component_name=target_component_name` into both the Startup prep command at
`:580` and the Shutdown at `:673`) — so nothing here SHALL weaken it

3.7 WHEN a cleanup, sweep or removal step hits a filesystem error, a stuck
advisory lock, or an unreadable/malformed owner marker THEN the system SHALL
CONTINUE TO fail open — log and proceed, exit 0 — never failing a Greengrass
deployment and never bricking the device

3.8 WHEN a model is in a transient state but its component is still installed —
staged with its load not yet requested, LOADING, FAILED with retries
outstanding — THEN the system SHALL CONTINUE TO retain its staged repository; a
transient state SHALL NOT be read as "removed" and SHALL NOT cause data loss

3.9 WHEN a component is being restarted by Greengrass (`STOPPING`, `INSTALLED`,
`STARTING`, `BROKEN`, or restarting because a dependency was in a bad state) THEN
it SHALL CONTINUE TO count as installed and its staged repository SHALL CONTINUE
TO be retained — the incident's `service-restart` cycles must not become sweep
targets

3.10 WHEN vision (Triton) models or LFV models are deployed, stopped or removed
THEN the system SHALL CONTINUE TO behave exactly as today; nothing here touches
`triton_model_repo`, the embedded Triton, or the LFV path

3.11 WHEN a model component's Shutdown runs `vllm_model_prep.py --cleanup` THEN
that `cleanup()` path SHALL CONTINUE TO behave exactly as it does today — owner
check, unload and `rmtree` under the per-model stage lock, leftover `.staging-*`
sweep, exit 0 on refusal or error. It was proven correct 19/19 in this incident
and SHALL NOT be modified by this fix

3.12 WHEN models whose components are still installed are reconciled THEN the
existing bounded 4-attempt retry budget, the 30 / 120 / 480 s schedule and the
validated KV-OOM unload -> reload recovery SHALL CONTINUE TO apply with identical
semantics; 2.5 changes only the *order and concurrency* in which candidates are
driven, never the per-candidate policy

3.13 WHEN any model's on-device state is removed THEN the contents of
`$HF_HOME/hub` SHALL CONTINUE TO survive, so a redeploy of the same model does
not pay the re-download again and no other component's shared weights are
affected

3.14 WHEN this fix is delivered THEN the component recipes, the published model
component recipes' lifecycle scripts, `src/docker-compose.yaml`, the
backend/frontend/edgemlsdk Dockerfiles, `src/backend/requirements.txt` and
`station_install/setup_station.sh` SHALL CONTINUE TO be unchanged — no
preservation-tracked file is expected to change (see *Rollout and verification*;
if the design needs one, it must be flagged and rebaselined in the same commit)

### Properties

```pascal
// Property: Fix Checking -- genuinely orphaned state converges
FOR ALL X WHERE isOrphanedStage(X) DO
  orphans <- { m IN (X.staged UNION X.temps) :
                 X.owner(m) IS NOT NONE AND X.owner(m) NOT IN X.installed }
  X'      <- convergenceSweep'(X)              // X.installed from IPC ListComponents
  FOR ALL m IN orphans DO
    ASSERT NOT repositoryExists(X', m)
    ASSERT NOT stagingTempExists(X', m)
    ASSERT m NOT IN keys(reportedModels(X'))
    ASSERT loadAttempts(reconcile'(X'), m) = 0
    ASSERT outcomeRecorded(X', m) IN {REMOVED, SKIPPED_WITH_REASON, FAILED_WITH_ERROR}
  END FOR
END FOR
```

```pascal
// Property: Fix Checking -- no candidate is charged for another's retries
FOR ALL X WHERE isHeadOfLineBlocked(X) DO
  R <- reconcile'(X)
  FOR ALL b IN X.candidates DO
    ASSERT firstLoadRequestDelay(R, b) < backoffBudget(anyOtherCandidate)
  END FOR
END FOR
```

```pascal
// Property: Preservation Checking -- the INSTALLED set is authoritative, and a
// non-root installed component is as protected as a root one. This property is
// the incident, expressed as a test.
FOR ALL X WHERE ownerInstalledButNotRoot(X, m) DO
  //   m staged, X.owner(m) IN X.installed, X.owner(m) NOT IN rootComponents(X)
  ASSERT isOrphanedStage(X) = FALSE
  X' <- convergenceSweep'(X)
  ASSERT repositoryExists(X', m)   = repositoryExists(X, m)
  ASSERT ownerMarker(X', m)        = ownerMarker(X, m)
  ASSERT tombstonePresent(X', m)   = tombstonePresent(X, m)
  ASSERT reportedStatus(X', m)     = reportedStatus(X, m)
END FOR
```

```pascal
// Property: Preservation Checking -- fail open on an absent or bad marker
FOR ALL X, m IN X.staged WHERE X.owner(m) IS NONE DO
  X' <- convergenceSweep'(X)
  ASSERT repositoryExists(X', m) = TRUE
  ASSERT outcomeRecorded(X', m)  = SKIPPED_WITH_REASON
END FOR
```

```pascal
// Property: Preservation Checking -- non-buggy device states are untouched
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

```pascal
// Property: Preservation Checking -- the Shutdown cleanup path is unmodified
FOR ALL m, c DO
  ASSERT cleanup'(model_name=m, component_name=c) = cleanup(model_name=m, component_name=c)
END FOR
```

Where **F** is the current (unfixed) device behavior and **F'** the fixed
behavior.

### Resolved evidence (was: Open Questions)

Every question the previous revision left open has been settled from CloudWatch
Logs, the Greengrass APIs and the repository.

- **Q1 — Does Greengrass run Shutdown when a component is dropped from a revised
  deployment? CONFIRMED, positively.** It does, and the published recipe carried
  it: `"Shutdown": {"Script": "python3 /aws_dda/vllm_model_prep.py --cleanup
  --model_name qwen3-5-9b --component_name
  model-vllm-qwen3-5-9b-jetson-xavier-jp7", "Timeout": 900, "requiresPrivilege":
  true, "runWith": {"posixUser": "root"}}`. Revision 53 is the clean
  demonstration: `merge-config … Removing services` -> `Cleaned directory` ->
  `delete-component-finish`, with no re-stage after.
- **Q2 — Was `--cleanup` refused by the ownership guard? REFUTED.**
  `filterPattern='"REFUSING"'` across all six `model-vllm-*` log groups returns
  **0 hits**. The guard (`vllm_model_prep.py:1210-1211`) compares the marker's
  `component_name` against `--component_name`, and both are the same per-target
  value by construction (`greengrass_publish.py:1036/:1049` pass one
  `component_name=target_component_name` into both the Startup prep command at
  `:580` and the Shutdown at `:673`). The marker recorded the same owner on all
  19 stagings. The `vllm-multi-arch-publish-conflict` renaming hypothesis does
  not apply.
- **Q3 — Did cleanup reach the removal but get killed or stall? REFUTED.** No
  stage-lock warning appears in any of the 420 log events, and `Cleaned
  directory` reached completion 19/19. Unload 5.9 s against a 300 s timeout;
  Shutdown 8.9 s against a 900 s recipe timeout.
- **Q4 — Did ordering / a post-cleanup re-stage matter? CONFIRMED**, and it is
  the mechanism: `20:44:05.299Z Cleaned directory` ->
  `20:44:05.320Z {STOPPING -> INSTALLED} ; {INSTALLED -> STARTING}` ->
  `20:44:05.418Z Staged vLLM model` (119 ms). The root cause sits one level
  above: the still-deployed workflow's HARD dependency
  (`workflow_packaging.py:1576`, `>=0.0.0` HARD at `:1617`) kept the component
  installed as a non-root dependency, and the restart trigger was
  `dependency aws.edgeml.dda.LocalServer.arm64JP7 was in a bad state`.
- **Q5 — What device-side signal authoritatively answers "which vLLM model
  components are currently deployed"? RESOLVED: Greengrass IPC
  `ListComponents`**, joined to staged repositories through each repository's
  `.dda_stage_owner.json` `component_name`. Already available:
  `recipe-arm64-jp7.yaml:63-65` grants `aws.greengrass#ListComponents` +
  `aws.greengrass#GetComponentDetails` on `resources: ['*']`, and
  `gg_utils.py:145 list_all_gg_components_with_details()` already returns every
  component with version and state over the process-wide IPC client. The
  deployment document / `effectiveConfig.yaml` is **REJECTED** as the primary
  signal even though the backend can read it — its ROOT components are exactly
  the set that omitted `qwen3-5-9b` while a workflow still needed it. The marker
  is the only device-side artifact mapping a model name to a component name, so
  it stays as the join key, and a missing or malformed marker means **retain**.
- **Q6 — What is the right treatment of the HF weights cache? RESOLVED, and
  already decided in the repo.** `docs/multi-runtime-inference.md` §22.1 (lines
  664-679) records that undeployment deliberately does not remove `$HF_HOME/hub`,
  for two reasons: a redeploy avoids the re-download, and safe automatic eviction
  would need cross-component reference counting. §22.2 documents the manual
  `rm -rf /aws_dda/hf_cache/hub/models--{org}--{name}` escape hatch. Requirement
  2.7 cites that decision rather than re-making it.

### What genuinely remains unresolved

- **(a) The ~11-minute reconciler delay figure is cloud-unverifiable.** The
  LocalServer UserComponent log group carries only the docker-compose wrapper's
  stdout, and a filter for `"qwen3-5-9b"` across it returns 0 hits. Confirming
  the magnitude needs an on-device `docker logs` capture, or shipping the
  container log to CloudWatch. The head-of-line *structure* is verified in code
  and does not depend on this figure.
- **(b) Whether the leftover directory is physically gone today.** Revision 53's
  `Cleaned directory` + `delete-component-finish` says it should be. One
  read-only `ls -la /aws_dda/dda_triton/vllm_model_repo/` on `jetson-thor1`
  settles it.
- **(c) Whether an orphaned `.staging-*` temp sibling ever existed** on this
  device. The same `ls` answers it, and the answer calibrates how much the temp
  sibling case matters to the sweep.
- **(d) Whether IPC `ListComponents` reports a component that is MID-REMOVAL.**
  This is the sweep's correctness boundary: a sweep running concurrently with a
  removal deployment must not delete a repository whose component is about to be
  legitimately re-staged, nor skip one whose component has already gone. The
  answer cannot be read out of the repository — it needs a real-hardware
  experiment (drive a removal deployment while polling `ListComponents`), per the
  workspace's on-device verification rule.

### Rollout and verification

This is a **device-side change**: the convergence sweep and the reconciler
ordering both live in the LocalServer backend (`src/backend/vllm_runtime/`,
`src/backend/utils/gg_utils.py`). Per `.kiro/steering/builds.md` it therefore
requires a component build and **end-to-end verification on real hardware of each
arch it touches before commit** — not only unit tests and flask-app container
runs. The on-device verification must cover: a genuinely orphaned repository
being swept; a still-installed non-root-dependency model **not** being swept
(the incident's configuration, i.e. Property 3 above, exercised for real); and
the backend staying healthy with no container restart for a sustained period
after the sweep runs.

No preservation-tracked file is expected to change — no `src/docker-compose.yaml`,
no backend/frontend/edgemlsdk Dockerfile, no `src/backend/requirements.txt`, no
recipe variant, no `station_install/setup_station.sh`. If the design ends up
needing one (for example a new IPC authorization policy in a recipe, which the
existing `cli:2` policy at `recipe-arm64-jp7.yaml:63-65` should make
unnecessary), that must be called out and its baseline rebaselined in the same
commit **before** the build is started, since the security preservation gate runs
after the ~1 h compile.
