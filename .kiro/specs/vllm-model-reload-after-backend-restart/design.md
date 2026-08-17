# vLLM Model Reload After Backend Restart Bugfix Design

## Overview

On jetson-thor1 (JP7/Thor, LocalServer.arm64JP7 1.0.6) the qwen3-vl-8b-instruct
model loaded successfully once, then a LocalServer backend container restart
(five awscrt aborts in a row, 21:55–22:33Z on 2026-08-16) silently destroyed the
in-process vLLM runtime — and NOTHING on the device ever re-issued the load. The
staged repository survived on disk, the Greengrass model component kept
reporting RUNNING, the backend reported healthy, the feature-config API reported
the model "LOADING" forever, and every generate request answered HTTP 409 until
a human restarted the model component. The defect is structural, not incidental:
the vLLM model load is a ONE-SHOT action in the model component's Startup script
(`vllm_model_prep.py` stage → `POST /load`), and once Startup exits 0 no code
path anywhere re-asserts the load, while the loaded engine lives in the backend
process (`VllmRuntimeManager`) that any restart discards. The 2026-08-17
amendment added a second, portal-side leg with the same incident fingerprint:
workflow packaging emits the UNSUFFIXED vLLM model component name
(`model-vllm-{safe_model_name}`) as a HARD dependency, and a stale JP6-era
artifact under that base name transitively dragged `LocalServer.arm64JP6`
1.0.59 onto the JP7 Thor.

The fix has two independent legs, matching the two defect groups in bugfix.md:

- **Device leg (the core — 2.1/2.2/2.3/2.4, 3.5):** a new
  **Vllm_Reconciler** started by `app.py`'s `start_vllm_runtime()` scans
  `VLLM_MODEL_DIR` at every backend start and re-drives the load of every
  staged, desired model through the SAME loopback model-control endpoint the
  component Startup uses (`POST /v2/repository/models/{m}/load`), sequentially,
  with failure isolation and a bounded retry schedule (Decision 1). An explicit
  operator unload writes an **Unload_Tombstone** marker inside the staged
  repository directory that suppresses reconciliation until the next component
  Startup/deployment re-stages the repository (which atomically replaces the
  directory and therefore the marker — Decision 2). The status surfaces become
  truthful: a tombstoned staged model reports STOPPED instead of an eternal
  LOADING, and a terminally failed reload reports FAILED with the retained
  backend reason (Decision 3). `vllm_model_prep.py` itself is NOT modified —
  its `LOAD_UNREACHABLE` diagnostic ("stays staged for the next LocalServer
  start") stops being a false promise because the reconciler makes it true.
- **Portal leg (1.6/2.6, 3.9):** `workflow_packaging.py`'s
  `resolve_model_components` stops short-circuiting on the vLLM record's
  singular `published_component` map (whose `component_name` is deliberately
  the arch-agnostic UNSUFFIXED base name). It resolves vLLM references
  per-architecture through the record's platform-suffixed
  Per_JetPack_Component entries — exactly the vision discipline — failing
  closed with an error naming the model and the uncovered architecture when a
  selected architecture has no suffixed coverage (including legacy records
  that carry only the unsuffixed base name). The unsuffixed base name is NEVER
  emitted as a dependency (Decision 5). A defense-in-depth arch-contradiction
  guard additionally refuses packaging when a resolved model component's own
  recipe HARD-depends on a LocalServer variant contradicting the workflow's
  target architecture (Decision 6).

Two explicit scope dispositions from bugfix.md are honored: the device-side
port-conflict diagnostic (1.5/2.5) is NOT implemented by this spec (Amendment
A1 — candidate split into its own spec; the coexistence scenario is prevented
at its source by 2.6; Decision 4 records the disposition and the recommended
shape), and the awscrt refcount abort stays out of scope (it is the restart
TRIGGER the reconciler makes survivable, not the defect).

Blast radius: the device leg is backend-container Python under
`src/backend/` (`vllm_runtime/`, `app.py`, `utils/feature_configs_utils.py`,
`endpoints/text_generation.py` state map) — full sequential component builds
(JP7 first; jetson-thor1 is the verification device) plus on-hardware
verification per `.kiro/steering/builds.md`. vLLM-free images (JP5 default,
x86) are inert by construction: the reconciler is only created inside
`start_vllm_runtime()`'s `VLLM_AVAILABLE` branch (Decision 7). **No
preservation-tracked file is touched** (no compose/Dockerfile/requirements/
recipe/setup_station changes — verified against the gate's pin list; no
baseline rebaselines expected) and no new Python dependencies. The portal leg
ships via a portal deploy sequenced strictly after the component builds
(builds.md: never portal-deploy while a component build runs).

## Glossary

- **Bug_Condition (C)**: a device state observation — one vLLM model on one
  device — where the model's runtime is vllm, its staged repository is present
  under `VLLM_MODEL_DIR`, its Greengrass component is RUNNING (Startup exited
  0, no `--cleanup`), a backend restart destroyed the in-process engine after
  the load, and no load is ever re-issued (`NOT X.load_reissued`)
- **Property (P)**: the desired behavior — after any backend restart, every
  staged, desired vLLM model is automatically re-driven through
  LOADING to READY (or FAILED with the retained reason), status surfaces stay
  truthful, explicit unloads stay honored, and workflow packaging emits only
  platform-suffixed vLLM model dependencies
- **Preservation**: first-deploy Startup semantics, failure isolation,
  ONNX/Triton vision paths, READY-model generate semantics, idempotent unload,
  vLLM-free image startup, component lifecycle semantics, port bindings, and
  vision/plugin packaging — all unchanged (bugfix.md 3.1–3.9)
- **VllmRuntimeManager**: `src/backend/vllm_runtime/manager.py` — owns every
  vLLM model in the backend process; per-model state machine
  STAGED → LOADING → READY | FAILED(reason), UNKNOWN for never-staged names;
  created EMPTY by `app.py`'s `start_vllm_runtime()` on every backend start
- **Companion runtime server**: `vllm_runtime/server.py` — the loopback
  Triton generate-extension + model-control HTTP server (uvicorn daemon
  thread, `127.0.0.1:8901` default, `VLLM_RUNTIME_PORT` env-overridable);
  `POST /v2/repository/models/{m}/load|unload` is the model-control surface
- **`VLLM_MODEL_DIR`**: `/aws_dda/dda_triton/vllm_model_repo` — root of every
  staged Triton_vLLM_Repository; survives container restarts (bind-mounted
  device filesystem); deliberately disjoint from the vision Triton's repo
- **`vllm_model_prep.py`**: `src/backend/dda_triton/vllm_model_prep.py` —
  the model component's Startup/Shutdown script: validate → rewrite weights
  sentinel → stage atomically → request load (Startup); unload → remove staged
  directory (`--cleanup`, Shutdown). The ONE-SHOT load driver on unfixed code
- **Vllm_Reconciler**: NEW `src/backend/vllm_runtime/reconciler.py` — daemon
  thread started by `start_vllm_runtime()`; scans `VLLM_MODEL_DIR` once per
  backend start and re-drives loads for staged, non-tombstoned models through
  the loopback model-control endpoint, sequentially, with bounded retries
- **Unload_Tombstone**: marker file `.dda_explicit_unload` written INSIDE a
  model's staged repository directory (`VLLM_MODEL_DIR/{model}/`) by an
  explicit unload; its presence suppresses reconciliation; removed by an
  explicit load request and (automatically) by the component Startup's atomic
  re-stage, which replaces the whole directory
- **`_VLLM_STATUS_MAP`**: `utils/feature_configs_utils.py` lines 138–144 —
  manager state → feature-config status; maps STAGED→"LOADING" on the
  documented assumption that "STAGED models have their load request on the
  way" — the assumption the bug broke and the reconciler restores
- **`_STATE_CATEGORY`**: `endpoints/text_generation.py` lines 362–367 — the
  Text_Generation_API's 409 state categories; STAGED and LOADING both map to
  "loading", which is what the workflow LLM binding's bounded poll rides
- **LLM loading budget**: `workflow_engine/output_bindings.py`
  `LLM_LOADING_BUDGET_SEC = 240` — the workflow binding re-POSTs on
  `409 {'state': 'loading'}` for at most 240 s, then fails terminally
- **Per_JetPack_Component**: a platform-suffixed vLLM model component
  (`model-vllm-{safe}-{target}`, e.g.
  `model-vllm-qwen3-vl-8b-instruct-jetson-xavier-jp7`), each HARD-depending on
  exactly one JetPack's LocalServer variant (vllm-multi-arch-publish design)
- **Unsuffixed base name**: `model-vllm-{safe_model_name}` — kept as the vLLM
  record's top-level `component_name` / `component_name-index` GSI key for
  legacy readers (`greengrass_publish.py` lines 1220–1226); an artifact under
  this name in Greengrass is what the unfixed packaging leg depends on
- **`resolve_model_components` / `model_component_dependencies`**:
  `edge-cv-portal/backend/functions/workflow_packaging.py` lines 1312 / 1441 —
  resolve workflow model references to published components and emit the HARD
  ComponentDependencies entries
- **Defect F / Defect G semantics**: the vision packaging disciplines
  preserved by 3.9 — divergent per-target names are OMITTED with a warning
  (recipe-global HARD deps on multiple variants are undeployable), and
  uncovered architectures FAIL CLOSED naming model + arch
- **jetson-thor1**: JP7/Thor verification device — the incident device

## Bug Details

### Bug Condition

**Formal Specification (from bugfix.md):**
```pascal
FUNCTION isBugCondition(X)
  INPUT: X — a device state observation (one vLLM model on one device)
  OUTPUT: boolean

  RETURN X.model.runtime = "vllm"
     AND X.model.staged_repo_present            // VLLM_MODEL_DIR/{name} on disk
     AND X.model.component_state = RUNNING       // Startup exited 0, no --cleanup
     AND X.backend_restart_occurred_after_load   // in-process engine destroyed
     AND NOT X.load_reissued                     // no reconciliation, no lazy load
END FUNCTION
```

On the unfixed tree, `NOT X.load_reissued` holds for EVERY such X: the only
load driver is the component Startup, which has already exited. The fixed tree
makes `NOT X.load_reissued` unreachable — the reconciler re-drives a load for
every staged, desired model at every backend start.

The portal leg (amendment clause 1.6) is a separate input class: a workflow
packaging run whose definition references a vLLM model. On the unfixed tree
every such run emits the unsuffixed base component name as a HARD dependency;
when a stale artifact exists under that name, the deployment closure silently
installs a wrong-arch LocalServer lineage.

### Examples

- **jetson-thor1 incident (the motivating event)**: qwen loaded READY at
  21:52:32Z (KV cache 37.20 GiB); five backend kills (awscrt aborts) later,
  the 22:33:11Z backend is healthy with `gpuActiveModels: 0, models: {}`;
  component RUNNING; feature-config "LOADING" forever; generate → 409 forever;
  workflow LLM nodes fail terminally after the 240 s poll budget. Recovery
  required a human component restart (Amendment A4). Expected: the 22:33:11Z
  backend re-drives the load itself; qwen is READY again within one engine
  load time, with truthful LOADING status during the reload.
- **Repeated-restart churn (Amendment A3)**: after the JP6 stack was removed,
  the awscrt abort fired 3 more times, each ~45–100 s after an engine spawn,
  each killing a just-loaded model. Expected: each new backend process starts
  a new reconciler that re-drives the load — the model converges to READY as
  soon as one load survives the churn (observed: the 4th, 23:46Z, was stable).
- **Explicit unload (3.5 / 2.4 contrast)**: an operator POSTs
  `/v2/repository/models/qwen/unload`; the repo stays staged. Expected: the
  unload succeeds and frees the engine (unchanged), a tombstone is written,
  the NEXT backend restart does NOT resurrect the model, and status reports
  STOPPED — until a component Startup/deployment re-stages the repo (marker
  replaced with the directory) or an explicit load request clears it.
- **`--cleanup`'d model (2.4, ¬C)**: Shutdown unloaded and removed the staged
  directory. Expected: byte-identical to today — nothing to scan, nothing
  resurrected.
- **Fresh deployment (3.1, ¬C interaction)**: a Greengrass deployment
  recreates the backend container AND restarts the model component; the
  component Startup requests the load AND the reconciler sees the staged repo.
  Expected: exactly ONE engine construction (manager idempotency +
  single-event-loop serialization, Decision 1); component exit-code semantics
  unchanged.
- **Portal leg (1.6 → 2.6)**: packaging workflow 421f8233 v7.0.0 (target
  arm64_jp7) referencing qwen. Unfixed: HARD dep on
  `model-vllm-qwen3-vl-8b-instruct` (unsuffixed, JP6-era artifact v1.0.0
  hard-depending on LocalServer.arm64JP6 >=1.0.0) → JP6 lineage dragged onto
  the Thor. Expected: HARD dep on
  `model-vllm-qwen3-vl-8b-instruct-jetson-xavier-jp7` only; a legacy record
  with no suffixed JP7 coverage fails packaging closed naming the model and
  arm64_jp7.
- **Edge case — vLLM-free image (3.6)**: JP5/x86 without the vllm wheel.
  Expected: `start_vllm_runtime()` returns None before any reconciler
  construction — the exact pre-feature startup sequence.
- **Edge case — reload genuinely fails (2.1/2.3)**: the staged weights are
  gone or the engine OOMs terminally. Expected: bounded retries (with the
  validated KV-OOM single unload→reload recovery per attempt), then FAILED
  with the retained backend reason — never an eternal "LOADING", never an
  unbounded retry storm (3.2).

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors (bugfix.md 3.1–3.9):**
- First-deploy Startup sequence (3.1): `vllm_model_prep.py` is NOT modified —
  validation defects → exit 1, `LOAD_UNREACHABLE` → exit 1 with the
  backend-container diagnostic, `LOAD_HTTP_ERROR` → exit 1 with the
  authoritative failure log, KV-cache OOM → single unload→reload recovery,
  atomic staging, no LocalServer restart. The reconciler coexists with the
  Startup load without double-loading (Decision 1).
- Failure isolation (3.2): the manager's per-model STAGED → LOADING →
  READY | FAILED(reason) isolation is untouched; the reconciler adds a
  BOUNDED retry schedule and never converts terminal FAILED into a storm.
- ONNX/Triton vision paths (3.3): no file under the vision Triton territory
  (`model_convertor.py`, `triton_setup.py`, `triton_edge_client.py`,
  `inference_runtimes.py`, TRITON_MODEL_DIR handling) changes.
- READY-model generate path (3.4): request validation, 409/422/502 mappings,
  streaming, multimodal handling — untouched (`server.py` generate routes and
  `manager.generate*` unchanged).
- Idempotent unload (3.5): `unload` keeps succeeding and freeing the engine
  from any state; the tombstone write is best-effort and additive.
- vLLM-free images (3.6): exact pre-feature startup sequence (Decision 7).
- Component lifecycle semantics (3.7): no recipe changes, no Startup-script
  semantics change, no LocalServer restart from a model component lifecycle.
- Port bindings (3.8): `VLLM_RUNTIME_HOST`/`VLLM_RUNTIME_PORT` constants and
  the server bind are untouched.
- Vision/plugin/no-model packaging (3.9): vision per-target resolution with
  the Defect F single-variant omission and Defect G fail-closed coverage,
  `dda.plugin.*` pinning, and the LocalServer single-variant discipline —
  byte-identical resolution results for every non-vLLM input.

**Scope:**
All inputs that do NOT involve (a) a staged vLLM model surviving a backend
restart, (b) an explicit vLLM unload's persistence semantics, or (c) a
workflow packaging run referencing a vLLM model, are completely unaffected.
This includes every vision model operation, every READY-model generate
request, every vLLM-free image, and every vision-only/plugin-only workflow
packaging run.

## Hypothesized Root Cause

> Not a hypothesis: the live-device diagnosis (2026-08-16 22:30–22:45Z), the
> addendum's verified checks, and the amendment's account evidence
> established both legs directly, and the code reading below confirms each
> mechanism. Section header kept per the bugfix design format.

### Device leg — the load is one-shot; the engine holder is restart-mortal; no reconciliation exists

1. **The only load driver is the component Startup, and it runs once**
   (defects 1.1, 1.2). `vllm_model_prep.py::prepare()` (lines 504–600) runs
   validate → stage → `request_load()` and exits; the recipe's Shutdown runs
   `--cleanup` (`cleanup()`, lines 602–626). Between those two lifecycle
   events NOTHING observes the backend or re-asserts the load. The script's
   own `LOAD_UNREACHABLE` diagnostic (`_request_load_attempt()`, lines
   434–441: "model '{}' stays staged for the next LocalServer start") promises
   an auto-load-on-start that exists NOWHERE in the codebase — the falsehood
   that pins the missing mechanism.
2. **The engine lives in the backend process; every restart starts empty**
   (defect 1.1). `app.py::start_vllm_runtime()` (line 295) constructs
   `manager = VllmRuntimeManager()` — a FRESH manager with an empty
   `_models` table — on every backend start, then starts the loopback server.
   No code scans `VLLM_MODEL_DIR` for staged repositories and no load is
   issued. The staged repo IS visible — `manager.list_models()` (line 221)
   and `state()` (line 209) report disk-staged repos as STAGED via
   `_repository_staged()` (line 241) — but nothing acts on that knowledge.
3. **No lazy load exists** (defect 1.3). `manager._ready_engine()` (line 508)
   raises `ModelUnavailableError` for any non-READY model; the server maps it
   to 409 (`server.py` exception handler). Unlike vision models (re-startable
   on demand via `start_model_triton` on UNKNOWN/UNAVAILABLE), a staged vLLM
   model answers 409 forever. The workflow LLM binding
   (`output_bindings.py`, `LLM_LOADING_BUDGET_SEC = 240`, line 1157) polls
   `409 {'state': 'loading'}` for 240 s and then fails terminally.
4. **The status surface asserts the missing mechanism exists** (defect 1.4).
   `feature_configs_utils._VLLM_STATUS_MAP` (lines 138–144) maps
   STAGED→"LOADING" with the comment "STAGED models have their load request
   on the way" — true at first deploy, false forever after a backend restart.
   `text_generation._STATE_CATEGORY` (lines 362–367) likewise maps
   STAGED→"loading" into the 409 body, so the binding's poll burns its budget
   against a load that is not coming. Every dashboard shows
   permanently-loading-but-healthy.

### Portal leg — packaging emits the arch-agnostic base name (amendment 1.6)

5. **The vLLM short-circuit hands the unsuffixed name straight to the
   dependency emitter.** `workflow_packaging.py::resolve_model_components()`
   (line 1312): the vLLM branch (lines 1376–1382) short-circuits on the
   record's singular `published_component` map and returns it verbatim —
   whose `component_name` `greengrass_publish.py` DELIBERATELY keeps as the
   UNSUFFIXED base name (`derive_vllm_component_name()`, lines 442–448:
   `model-vllm-{safe_model_name}`; record write-back lines 1215–1262: "The
   top-level component_name attribute stays the UNSUFFIXED base name, which
   is the key the deployment gate's component_name-index GSI resolves the
   record by"). `model_component_dependencies()` (line 1441) then emits
   `{value['component_name']}` verbatim as a HARD, unpinned (`>=0.0.0`)
   dependency. The per-arch suffixed truth is RIGHT THERE in the same record
   — `published_component['components']` carries one entry per
   Per_JetPack_Component with `component_name`, `target`, and
   `architecture` — and is never consulted.
6. **The account carried a stale artifact under the base name.** JP6-era
   `model-vllm-qwen3-vl-8b-instruct` v1.0.0 HARD-depends on
   `aws.edgeml.dda.LocalServer.arm64JP6 >=1.0.0`; workflows 421f8233
   v6.0.0/v7.0.0 depended on the base name, so the single JP7 thing
   deployment's dependency closure installed LocalServer.arm64JP6 1.0.59
   beside JP7 on the Thor (Amendment A1 — refuting the two-deployments
   hypothesis).

## Design Decisions

### Decision 1 — Reconciliation mechanism (2.1): startup scan + re-drive through the loopback model-control endpoint, sequential, bounded

**Decision:** a new `Vllm_Reconciler` (`src/backend/vllm_runtime/reconciler.py`)
is constructed and started by `start_vllm_runtime()` immediately after the
runtime server is up and the manager is installed. It runs on ONE daemon thread
(`vllm-reconciler`), takes ONE snapshot of reload candidates at startup —
`manager.list_models()` entries in state STAGED whose repository carries no
Unload_Tombstone — and, for each candidate SEQUENTIALLY, re-drives the load by
POSTing the SAME loopback model-control endpoint the component Startup uses
(`POST http://127.0.0.1:{VLLM_RUNTIME_PORT}/v2/repository/models/{m}/load`,
`LOAD_REQUEST_TIMEOUT_SECONDS`-class timeout). Per model: HTTP 200 → READY,
done; an authoritative failure → bounded retry over
`RECONCILE_RETRY_BACKOFF_SECONDS = (30, 120, 480)` (4 attempts total), with the
validated KV-cache-OOM single unload→reload recovery applied per attempt
(mirroring `request_load()`'s marker-driven cycle — the post-restart reload IS
the "first load after a runtime restart" case that recovery was validated
for); after exhaustion the model is LEFT in FAILED with its retained reason and
one prominent ERROR names the model, the final reason, and that automatic
retries are exhausted. One model's failure never stops the scan (3.2). The
whole thread body is wrapped so a reconciler crash is logged and never touches
the backend (the `start_vllm_runtime` containment convention).

**Rationale — why the loopback HTTP endpoint and not direct `manager.load()`
calls:**
- **Engine/event-loop affinity.** `AsyncLLMEngine` binds its background loop
  to the event loop it is created on; generate requests run on the runtime
  server's uvicorn loop. Driving the load through the HTTP endpoint executes
  `manager.load()` on exactly that loop — the only correct place. A
  reconciler-side `asyncio.run(manager.load(...))` would create the engine on
  a throwaway loop and break every subsequent generate.
- **Double-load safety with ZERO manager changes.** During a deployment the
  component Startup and the reconciler can both request the same model's
  load. `manager.load()` is idempotent for LOADING/READY entries, and —
  decisive — the load body runs WITHOUT an await point between entry
  creation, `parse_repository`, the LOADING transition, and the (synchronous)
  engine construction, so two load coroutines on the single uvicorn event
  loop serialize completely: exactly one engine construction per model, by
  construction. This holds only if EVERY load arrives via the HTTP endpoint —
  which this decision makes an invariant (documented in the reconciler
  module). The reconciler additionally re-checks `manager.state()` right
  before each POST and skips LOADING/READY names.
- **Byte-identical load semantics.** The endpoint path is the exact path the
  component Startup exercises (validation, failure isolation, status
  transitions, 409-FAILED body with retained reason), so reconciliation
  cannot drift from first-deploy behavior (3.1) — it is literally the same
  code re-invoked from inside the container.
- **Sequential, not concurrent:** engine loads are GPU-memory-hungry and
  today's reality is serial (one component Startup at a time); concurrent
  reloads would invite exactly the KV-cache OOM class the single-cycle
  recovery exists for. Multi-model devices reload in sorted name order —
  deterministic and testable.
- **One-shot scan, not a poller:** new stagings after backend start are the
  component Startup's job (it requests its own load); the reconciler's only
  mission is the restart-orphaned staged set, which is fully known at start.
  A perpetual poller would add a second, permanent load driver and new race
  surface for zero requirement coverage. Repeated restarts self-heal: every
  new backend process runs a fresh scan (the Amendment A3 churn case
  converges as soon as one load survives).
- **Retry schedule shape:** the incident showed engine-killing churn 45–100 s
  after spawn; (30, 120, 480) gives a fast second attempt, then waits out
  churn windows, staying bounded (~10.5 min worst case) per 3.2. A model that
  keeps failing ends FAILED-with-reason — visible, truthful (2.3), and
  recoverable by component restart exactly as today.
- **Rejected — lazy load in `_ready_engine`:** loading a 30+ GiB engine from
  inside a generate request handler couples request latency/timeouts to
  engine construction, changes the READY-path contract (3.4 risk), and still
  leaves the model unloaded until someone asks. Reconciliation restores the
  desired state proactively and keeps `_ready_engine` untouched.
- **Rejected — component-side liveness watchdog (recipe change):** a model
  component script polling the backend and re-running the load would change
  deployed recipe semantics (3.7 forbids), multiply per-model pollers, and
  put the fix on the wrong side of the process boundary: the entity that
  LOST the state (the backend) is the one that knows it restarted.
- **Rejected — persisting the manager's model table:** the staged repository
  IS the desired-state record (bugfix.md defines "desired" as
  staged-and-not-cleaned-up); duplicating it into a state file adds a second
  source of truth to keep consistent with `--cleanup` for no gain. The
  tombstone (Decision 2) covers the one case where "staged" and "desired"
  diverge.

### Decision 2 — Unload tombstone (3.5): marker file inside the staged repository, cleared by explicit load or re-stage

**Decision:** an explicit unload through the model-control endpoint
(`manager.unload()`) writes `VLLM_MODEL_DIR/{model}/.dda_explicit_unload`
(constant `UNLOAD_TOMBSTONE_NAME` in `vllm_runtime/constants.py`; content: a
small JSON with a UTC timestamp, for triage only) when — and only when — the
repository is still staged. The write is best-effort: a failure is logged and
the unload still succeeds and frees the engine (3.5 is categorical).
`manager.load()` removes the marker first thing — an explicit load request
re-arms reconciliation. The reconciler skips tombstoned repositories. The
component Startup's re-stage clears the marker with ZERO prep changes:
`stage_repository()` (lines 265–297) replaces the final directory wholesale
(`shutil.rmtree(final_dir); os.rename(staged_tmp, final_dir)`), so the marker
dies with the old directory — exactly 3.5's "suppresses reconciliation until
the next component Startup/deployment re-stages". `--cleanup` removes the
whole directory, marker included — no litter.

**Rationale:**
- **In-repo placement is self-cleaning.** A sibling marker
  (`VLLM_MODEL_DIR/.tombstone-{model}`) would survive the atomic re-stage and
  force an edit to `vllm_model_prep.py` to clear it — the file this design
  deliberately leaves untouched (3.1). In-repo placement gets the
  "re-stage re-arms" semantics for free from the existing atomic-replace
  staging.
- **Validation tolerates it (verified).** `vllm_runtime/repository.py::
  parse_repository()` checks only that `config.pbtxt` (declaring backend
  vllm) and `1/model.json` exist and parse — it does NOT reject extra
  entries — so a tombstoned repo still loads normally when explicitly asked.
  `vllm_model_prep.py::validate_repository()`'s strict exact-layout check
  runs against the UNARCHIVED SOURCE, which never carries the marker.
- **KV-OOM recovery is unaffected:** `request_load()`'s recovery cycle is
  unload → load; the unload writes the marker and the immediately following
  load clears it. Net effect: none, and the interleaving is unit-tested.
- **Every model-control unload tombstones (not just "operator" unloads):**
  the endpoint cannot distinguish an operator from a script, and every caller
  of `/unload` is expressing "stop serving this model". Component Shutdown's
  unload is followed by directory removal (marker moot); prep's recovery
  unload is followed by an immediate load (marker cleared). The semantics
  compose correctly for every existing caller — verified case by case above.
- **Rejected — TTL/time-boxed suppression:** "how long an explicit unload
  holds" (the 3.5 open question) is answered structurally, not temporally:
  it holds until a component Startup/deployment re-stages or an explicit
  load re-arms. A TTL would resurrect models at an arbitrary deadline no
  operator chose.

### Decision 3 — Truthful status (2.3): STAGED stays "LOADING" (now true by construction), tombstoned reports STOPPED, terminal reload failure reports FAILED-with-reason

**Decision:** three coordinated, additive status changes.

1. **`ModelState.UNLOADED` (new manager state, reporting-only):**
   `manager.state()` / `list_models()` report a staged-but-tombstoned,
   untracked model as `UNLOADED` instead of `STAGED`; `_ready_engine()`'s 409
   status for such names likewise carries UNLOADED. No transition logic
   changes — UNLOADED is derived from disk state exactly the way STAGED
   already is.
2. **`_VLLM_STATUS_MAP` gains `"UNLOADED": "STOPPED"`** — reusing the
   existing device status vocabulary (LFV models already report STOPPED, so
   every consumer renders it). STAGED→"LOADING" is RETAINED and its comment
   is corrected: the assumption "the load request is on the way" is now
   guaranteed by the reconciler (first deploy: the component Startup's load;
   after restart: the reconciler's re-drive) — bounded time in both cases.
3. **`text_generation._STATE_CATEGORY` gains
   `ModelState.UNLOADED.value: "unloaded"`** — the workflow binding's 409
   poll rides only `"loading"`, so a generate against an explicitly unloaded
   model fails fast and truthfully instead of burning the 240 s budget.

Terminal reload failure needs NO new mapping: the manager's `_fail()` path
already retains the reason and `_VLLM_STATUS_MAP` already reports
FAILED-with-reason; the reconciler simply stops retrying (Decision 1), so the
FAILED state is what the surfaces show — never an indefinite "LOADING" with no
load in flight. The 2.2 reload-window behavior also needs no new code:
STAGED/LOADING already map to the "loading" 409 category, and the reconciler
guarantees a load genuinely follows.

**Rationale:** minimal-diff truthfulness. The bug made STAGED→LOADING a lie;
the fix restores the invariant rather than redesigning the status vocabulary.
The only genuinely NEW observable state (explicitly-unloaded-but-staged) gets
the one new value; both new mappings are additive (3.4 — no renames, removals,
or type changes).

### Decision 4 — Port-conflict diagnostic (2.5): NOT implemented in this spec (scope disposition), recommended shape recorded

**Decision:** requirement legs 1.5/2.5 are NOT implemented by this spec,
honoring bugfix.md Amendment A1's scope disposition verbatim: the coexistence
scenario is prevented at its source by the packaging leg (2.6), and the
device-side diagnostic remains defense-in-depth, a candidate split into its
own spec.

**Recorded shape for the future spec (so the investigation is not lost):**
detect `EADDRINUSE`/`[Errno 98]` at the two bind sites (the backend's uvicorn
`server.serve()` in `app.py::main()` and `VllmRuntimeServer.start()`'s
startup-failure path), log ONE explicit diagnostic naming the port, the owner
uncertainty, and the LocalServer-lineage-coexistence hypothesis ("another
LocalServer lineage on this device may own 127.0.0.1:{port} — check
`sudo docker ps` for a second flask-app lineage and the thing deployment's
component list"), and exit after a bounded backoff (e.g. 300 s) instead of
crash-looping at Greengrass/compose cadence — cutting the ~60 s IPC churn
observed at RestartCount=61. Note: `start_vllm_runtime()`'s containment
already swallows an 8901 bind failure today (the runtime server failure never
crashes the backend); the observed JP6 crash-loop was the MAIN backend port
path. The future spec must re-verify which bind actually raised on 1.0.59.

**Rationale:** the requirements document is authoritative and user-approved;
its amendment explicitly moves 2.5 out of this spec's implementation scope.
Implementing it anyway would widen the on-hardware verification matrix
(deliberate dual-lineage deployments) for a scenario 2.6 now prevents at
packaging time.

### Decision 5 — Packaging leg (2.6): resolve vLLM references per-architecture through platform-suffixed Per_JetPack entries; fail closed; never the base name

**Decision:** `resolve_model_components()` drops the verbatim short-circuit on
the vLLM singular map. A record in the vLLM shape (a `published_component`
dict is present) resolves per selected architecture from its platform-suffixed
evidence:

1. **Primary source — `published_component['components']`** (the
   Per_JetPack_Component entries written by the multi-arch publish): entries
   with a non-empty string `component_name` whose `architecture` (an
   `arm64_jpN` arch id — the workflow's own vocabulary) is in the selected
   archs contribute their suffixed names.
2. **Secondary source — the record's plural `published_components`** entries
   with `status == 'published'`, matched on `target` against the selected
   archs' PRIMARY publish-target id (`ARCH_TO_PUBLISH_TARGET[arch]` — the
   `jetson-xavier-jpN` ids, which are exactly `packaging.VLLM_ARCH_TO_TARGET`'s
   values for jp5/jp6/jp7). The vision-only extra acceptance
   (`ARCH_TO_EXTRA_PUBLISH_TARGETS`, `onnx-jetson-xavier-jp7`) deliberately
   does NOT apply to vLLM records.
3. **Coverage gate — fail closed:** every selected architecture must be
   covered by at least one suffixed entry from either source; otherwise
   `PackagingError` naming the model AND the uncovered architecture(s), with
   remediation ("re-publish the model for every selected architecture — this
   record predates per-JetPack vLLM components" for legacy records that carry
   only the unsuffixed base name). The unsuffixed base `component_name` is
   NEVER a fallback and NEVER appears in a resolved value.
4. **Resolved value shape — a SET of suffixed names**, i.e. the vision shape.
   `model_component_dependencies()` then needs NO change: one name → one HARD
   entry; multiple distinct per-target names (multi-arch selection) → the
   existing Defect F omission-with-warning discipline applies, which is
   CORRECT for vLLM too — a recipe-global HARD dep on two Per_JetPack
   components (each hard-depending on its own LocalServer lineage) is
   undeployable on any single device and is precisely the incident's failure
   shape. Distinct-model dedupe and the unpinned `>=0.0.0` requirement are
   untouched.

**Rationale:**
- The suffixed truth already lives in the record (`components` entries carry
  `component_name` + `architecture`; the plural list carries `target`) — the
  fix is to consult it, not to invent new publish metadata.
- Matching primarily on `architecture` avoids introducing another mirrored
  target map (the Lambdas cannot import `packaging.py`; every mirrored map is
  a documented drift risk). The plural-entry `target` match reuses the map
  workflow_packaging ALREADY maintains for vision.
- Reusing the vision set-shape means the dependency emitter, the Defect F/G
  disciplines, and their existing test suites keep operating on one code
  path — the strongest 3.9 guarantee available.
- Fail-closed matches the file's every other gate (plugins, LocalServer
  variant, vision coverage) and the amendment's explicit requirement: a
  legacy unsuffixed-only record MUST become a packaging error, not a silent
  arch-agnostic dependency.

### Decision 6 — Arch-contradiction guard: REFUSE with a clear error when a resolved model component's LocalServer dependency contradicts the workflow target architecture

**Decision:** implement the guard (the amendment's open design question) as
refuse-with-clear-error, scoped as defense-in-depth: after model resolution,
for each resolved model component name, packaging fetches the component's
LATEST version recipe from Greengrass (`get_component`, one call per resolved
component, alongside the existing `list_components` version-resolution
traffic), reads its `ComponentDependencies` keys matching
`aws.edgeml.dda.LocalServer.*`, and compares each against the selected
architectures' variants (`ARCH_TO_LOCAL_SERVER_COMPONENT`). A LocalServer
variant that serves NONE of the selected architectures →
`PackagingError` naming the model component, the contradicting LocalServer
variant, and the workflow's target architecture(s). A recipe that names no
LocalServer dependency, or a guard READ failure (throttle, transient API
error), logs a warning and proceeds — the guard must never make packaging
flakier than the primary fix, which already prevents the incident class.

**Rationale for refuse (vs warn-only):** the incident proved the blast radius
of a wrong-arch model dependency is a wrong-arch LocalServer LINEAGE on a
production device — crash-loops, IPC churn, and (pre-2.1) permanently lost
models. Refusing at packaging time is the cheapest point on the timeline to
stop it, the error is precisely actionable (re-publish/repoint the model
component), and false positives are structurally implausible: a
Per_JetPack_Component's LocalServer dependency IS its architecture identity
(vllm-multi-arch publish invariant, property-tested in
`test_vllm_multi_arch_publish_*`). **Rationale for fail-open on read
failure:** the guard is secondary; Decision 5 already guarantees suffixed
names, so a missed guard check degrades to exactly the post-Decision-5
baseline, whereas failing packaging on a Greengrass API blip would be a new
reliability regression with no defect it prevents.

### Decision 7 — vLLM-free images stay inert (3.6): reconciler exists only behind `VLLM_AVAILABLE`

**Decision:** `reconciler.py` lives in `vllm_runtime/` (imports no `vllm`,
like the rest of the package), and is imported and constructed ONLY inside
`start_vllm_runtime()`'s try block, which returns `None` before any of that
when `VLLM_AVAILABLE` is false. No module-scope side effects, no new env
knobs, no thread on vLLM-free images — byte-identical pre-feature startup.
A reconciler construction/start failure is caught by the existing containment
(`[VLLM RUNTIME STARTUP FAILED]` semantics preserved: the runtime server
result still gates health exactly as before; a dead reconciler never flips
the backend unhealthy).

**Rationale:** 3.6 is categorical, and the existing `VLLM_AVAILABLE` +
containment pattern is the proven mechanism (it already kept the runtime
server, text-generation router, and status merge off vLLM-free images).

## Correctness Properties

Property 1: Bug Condition - A Backend Restart Never Silently Orphans a Staged vLLM Model

_For any_ set of staged vLLM repositories under `VLLM_MODEL_DIR` whose models
are desired (staged by a successful Startup, not `--cleanup`'d, not
tombstoned) and _for any_ backend restart (modeled host-side as constructing a
fresh manager + runtime server + reconciler over the surviving directory
tree), the fixed system SHALL re-issue a load for EVERY such model —
`NOT X.load_reissued` is unreachable — driving each through LOADING to READY
(fake engine factory succeeds) or to FAILED with the retained backend reason
(factory raises); and _for any_ generate request naming a model during the
reconciliation window, the response SHALL be the existing 409 state-info
mapping with the "loading" category, with the same request path serving
normally once the reload completes. On the UNFIXED tree the exploration form
of this property fails: the fresh manager holds zero models, no load request
is ever observed, generate answers 409 forever, and the feature-config status
reports "LOADING" indefinitely — the incident's exact fingerprint.

**Validates: Requirements 2.1, 2.2**

Property 2: Preservation - Everything Outside the Bug Condition Is Unchanged

_For any_ input where the bug condition does NOT hold, the fixed tree SHALL
produce the same result as the original tree: the first-deploy Startup
sequence byte-identical (`vllm_model_prep.py` unmodified — hash-pinned;
validation/exit-code/diagnostic/KV-OOM-recovery semantics pinned by the
existing suites; a concurrent component-Startup load and reconciler load
construct exactly ONE engine), per-model failure isolation and the
STAGED→LOADING→READY|FAILED machine untouched, ONNX/Triton vision paths and
`model_convertor.py` untouched, the READY-model generate path (validation,
409/422/502 mappings, streaming, multimodal) unchanged, `unload` succeeding
idempotently and freeing the engine from any state, vLLM-free images running
the exact pre-feature startup sequence (no reconciler, no new imports, no new
failure modes), no recipe or component-lifecycle semantics change, the
127.0.0.1:8901 (`VLLM_RUNTIME_PORT`) binding unchanged, and — portal side —
vision-only/plugin-only/model-free workflow packaging resolving and emitting
ComponentDependencies deep-equal to the unfixed output (Defect F omission,
Defect G fail-closed coverage, `dda.plugin.*` pinning, LocalServer
single-variant discipline).

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9**

Property 3: Fix Checking - Status Surfaces Are Truthful Within Bounded Time

_For any_ combination of manager model states, staged repositories, and
tombstones, the reported status (feature-config `_VLLM_STATUS_MAP` merge and
the Text_Generation_API 409 category) SHALL reflect reality: "LOADING" is
reported ONLY for models whose load is genuinely in flight or queued behind
the reconciler's bounded drive (STAGED-and-desired or LOADING); a
staged-but-tombstoned model reports STOPPED (feature-config) / "unloaded"
(409 category) — never LOADING; a model whose reload terminally failed
reports FAILED with the retained backend reason after the bounded retry
schedule exhausts — never an indefinite LOADING with no load in flight; and a
READY model reports READY. The reconciler SHALL issue at most the bounded
schedule of load attempts per model (with at most one KV-OOM unload→reload
recovery per attempt) and one model's failures SHALL never affect another
model's reconciliation.

**Validates: Requirements 2.3, 3.2**

Property 4: Fix Checking - Tombstone Semantics Across Unload/Load/Re-stage/Cleanup Sequences

_For any_ sequence of operations on one model drawn from {explicit unload,
explicit load, component-Startup re-stage (atomic directory replace),
`--cleanup` (directory removal), backend restart}, reconciliation after a
restart SHALL reload the model if and only if its repository is staged AND
the most recent tombstone-affecting operation re-armed it (explicit load or
re-stage) rather than suppressed it (explicit unload): an unload always
succeeds and frees the engine, writes the tombstone when the repo is staged
(best-effort — a marker write failure never fails the unload), a re-stage or
explicit load clears it, and a `--cleanup`'d model is NEVER resurrected
(nothing staged, nothing scanned). The KV-OOM recovery interleaving
(unload immediately followed by load) is net-neutral.

**Validates: Requirements 2.4, 3.5**

Property 5: Fix Checking - Workflow Packaging Emits Only Platform-Suffixed vLLM Model Dependencies

_For any_ vLLM model registry record shape (modern records with per-JetPack
`components` entries and plural `published_components`; intermediate records
with only one evidence source; legacy records carrying only the unsuffixed
base `component_name`) and _for any_ non-empty selected architecture set, the
fixed `resolve_model_components` + `model_component_dependencies` SHALL emit
only platform-suffixed Per_JetPack_Component names covering the selected
architectures — the unsuffixed base name NEVER appears in any resolved value
or emitted dependency — and SHALL fail closed with a `PackagingError` naming
the model and the uncovered architecture(s) whenever any selected
architecture lacks suffixed coverage (legacy unsuffixed-only records
included); multi-arch selections resolving to multiple distinct suffixed
names follow the existing Defect F omission-with-warning discipline; and the
arch-contradiction guard SHALL refuse packaging (naming the model component,
its LocalServer variant, and the target architecture) when a resolved
component's recipe HARD-depends on a LocalServer variant serving none of the
selected architectures, while a guard READ failure logs and proceeds. On the
UNFIXED tree the exploration form fails: the incident record shape (legacy
unsuffixed `model-vllm-qwen3-vl-8b-instruct` beside a suffixed JP7 record)
resolves to the base name and emits it as a HARD dependency.

**Validates: Requirements 2.6** (exploration form demonstrates defect 1.6)

## Fix Implementation

### Changes Required

**File 1 — NEW `src/backend/vllm_runtime/reconciler.py` (device leg, the core)**

```python
#: Bounded post-restart retry schedule per model (Decision 1): fast second
#: attempt, then wait out deployment/abort churn windows; ~10.5 min worst
#: case, then the model is LEFT in FAILED with its retained reason (3.2).
RECONCILE_RETRY_BACKOFF_SECONDS = (30, 120, 480)

class VllmReconciler:
    """Re-drives the load of every staged, desired vLLM model after a
    backend restart (spec: vllm-model-reload-after-backend-restart, 2.1).

    INVARIANT: every load is issued through the loopback model-control
    endpoint — never manager.load() directly — so engine construction
    happens on the runtime server's event loop and all load requests
    serialize there (Decision 1)."""

    def __init__(self, manager, port=VLLM_RUNTIME_PORT,
                 backoff=RECONCILE_RETRY_BACKOFF_SECONDS,
                 request_fn=None):   # injectable for tests
        ...

    def start(self):
        # daemon Thread(name="vllm-reconciler", target=self._run); the
        # entire body is try/except-contained: a reconciler failure is
        # logged and never touches the backend or the runtime server.
        ...

    def _candidates(self):
        # ONE snapshot: manager.list_models() entries in STAGED state
        # (UNLOADED — tombstoned — entries are already excluded by the
        # manager, Decision 3), sorted by name for deterministic order.
        ...

    def _reconcile_one(self, model_name):
        # skip if manager.state() is LOADING/READY by now (component
        # Startup got there first — the fresh-deploy case);
        # POST /v2/repository/models/{m}/load (LOAD_REQUEST_TIMEOUT-class
        # timeout); 200 -> done; authoritative failure -> KV-OOM markers?
        #   -> one unload -> reload recovery cycle (mirrors
        #      vllm_model_prep.request_load, validated on-device);
        # retry over the backoff schedule; exhausted -> prominent ERROR
        # naming the model, the retained reason, and that automatic
        # retries are exhausted (status stays FAILED — truthful, 2.3).
        # Per-model try/except: one model's failure never stops the scan.
        ...
```

Uses `requests` (an existing backend dependency) against
`127.0.0.1:{port}`. Imports nothing from `vllm` (package convention).

**File 2 — `src/backend/vllm_runtime/manager.py` (edit; tombstone + UNLOADED state)**

- `ModelState.UNLOADED = "UNLOADED"` (reporting-only state, Decision 3).
- New private helpers: `_tombstone_path(model_name)`
  (`self.model_dir / model_name / UNLOAD_TOMBSTONE_NAME`),
  `_tombstoned(model_name) -> bool`.
- `state()` / `list_models()`: where a staged, untracked repo today reports
  STAGED, report `UNLOADED` when tombstoned. `_ready_engine()`'s synthesized
  non-READY status likewise.
- `load()`: first action, best-effort `_clear_tombstone(model_name)`
  (explicit load re-arms reconciliation; failure to remove logs and
  proceeds).
- `unload()`: after the existing engine shutdown/reclaim, best-effort
  tombstone write when `_repository_staged(model_name)` — wrapped so any
  filesystem error is logged and the unload return value/semantics are
  byte-identical (3.5).

**File 3 — `src/backend/vllm_runtime/constants.py` (edit; one constant)**

`UNLOAD_TOMBSTONE_NAME = ".dda_explicit_unload"` with a docstring naming this
spec and the re-stage-clears-it mechanism.

**File 4 — `src/backend/app.py` (edit; start the reconciler)**

In `start_vllm_runtime()`, after `feature_configs_utils.set_vllm_manager(manager)`:

```python
from vllm_runtime.reconciler import VllmReconciler
VllmReconciler(manager).start()   # inside the existing try: containment
logger.info("vLLM reconciler started (staged-model reload after restart).")
```

Return value, health gating (`health.set_vllm_server`), and the
`VLLM_AVAILABLE` early return are untouched (3.6, Decision 7).

**File 5 — `src/backend/utils/feature_configs_utils.py` (edit; one mapping + comment)**

`_VLLM_STATUS_MAP` gains `"UNLOADED": "STOPPED"`; the STAGED→"LOADING"
comment is corrected to name the reconciler as the guarantee (first deploy:
component Startup's load; after restart: the reconciler) instead of the
broken "on the way" assumption. `get_features_vllm()` logic is unchanged
(FAILED reason retention already exists).

**File 6 — `src/backend/endpoints/text_generation.py` (edit; one mapping)**

`_STATE_CATEGORY` gains `ModelState.UNLOADED.value: "unloaded"` — truthful
fast failure for generates against explicitly unloaded models (the workflow
binding polls only on "loading"). All other categories unchanged.

**File 7 — `edge-cv-portal/backend/functions/workflow_packaging.py` (edit; portal leg)**

In `resolve_model_components()` (lines 1376–1382 today): replace the
singular-map verbatim short-circuit with per-arch suffixed resolution
(Decision 5) — a helper `_resolve_vllm_components(record, archs, label)`
returning a SET of suffixed names or raising `PackagingError` (model +
uncovered archs, legacy-record remediation text). `model_component_dependencies()`
is UNCHANGED (the set shape flows through the existing vision path,
inheriting Defect F omission and dedupe). New `_model_arch_contradiction_guard(
greengrass, resolved, archs)` (Decision 6) invoked from the packaging flow
after resolution: `get_component` per resolved name (latest version), parse
recipe `ComponentDependencies` for `aws.edgeml.dda.LocalServer.*`, refuse on
contradiction, warn-and-proceed on read failure.

**File 8 — NEW test suites**

- `test/backend-test/vllm_model_reload/` — device-leg exploration,
  fix-check, and preservation tests (see Testing Strategy); fake engine
  factory, temp `VLLM_MODEL_DIR` trees, real `VllmRuntimeServer` on an
  ephemeral port (or FastAPI TestClient where no socket is needed).
- `edge-cv-portal/backend/tests/test_vllm_workflow_arch_dependency_exploration.py`
  / `..._properties.py` / `..._units.py` — portal-leg tests (moto
  `aws_stack` conftest fixture, the `test_vllm_multi_arch_publish_*`
  conventions).

**Explicitly NOT changed:** `src/backend/dda_triton/vllm_model_prep.py`
(3.1 — its `LOAD_UNREACHABLE` "stays staged for the next LocalServer start"
diagnostic becomes TRUE via this fix; hash-pinned in the preservation suite),
`vllm_runtime/repository.py`, `vllm_runtime/server.py` (all routes and
mappings untouched — the reconciler is a client of the existing load
endpoint), `workflow_engine/output_bindings.py` (the 240 s poll now rides a
real reload), `model_convertor.py` and every vision-Triton file, all recipes
and the recipe generator (`greengrass_publish.py` untouched — the unsuffixed
GSI key stays for legacy readers; only the CONSUMER stops depending on it),
`deployments.py`, `src/docker-compose.yaml`, all Dockerfiles,
`src/backend/requirements.txt` (no new dependencies) — and therefore **no
security-preservation baseline rebaselines** (verified against the gate's pin
list: compose/Dockerfiles/requirements/recipes/setup_station.sh — none
touched).

## Cross-Spec Documentation Consistency

| Document | Relationship to this fix | Action |
|---|---|---|
| `.kiro/specs/vllm-jp7-engine-cuda-init/bugfix.md` | Sibling: the 21:51Z first-load CUDA failure leg of the incident; explicitly out of this spec's scope | No change — remains the CUDA-init authority |
| `.kiro/specs/csi-nvargus-optional/` | Sibling: the nvargus Error(89) degraded window that caused the first-load failure; watchdog mitigations | No change |
| `.kiro/specs/model-gpu-fallback-visibility/` | Complementary visibility work on the vision leg; its `gpu-status` endpoint provided the `gpuActiveModels: 0, models: {}` incident evidence | No change |
| `edge-deploy-reliability` spec (Defect D) | Authority for `vllm_model_prep.py`'s LOAD_UNREACHABLE/LOAD_HTTP_ERROR classifications — preserved verbatim (file unmodified) | No change |
| `vllm-sizing-and-packaging-errors` spec | Authority for the KV-OOM markers + single unload→reload recovery — the reconciler REUSES the marker semantics per attempt | No change; reuse noted here |
| `vllm-multi-arch-publish-conflict` spec | Authority for the Per_JetPack_Component publish shape (`components` entries, suffixed names, per-JetPack LocalServer deps) this fix resolves against; its property suites are the guard's invariant evidence | No change |
| `vision-model-packaging-regression` / Defect F/G semantics in `workflow_packaging.py` | The vision resolution disciplines 3.9 pins; the vLLM fix adopts the same resolved-value shape | No change; adoption noted here |
| The awscrt refcount abort (known follow-up, no spec yet) | The restart TRIGGER this fix makes survivable; explicitly out of scope | No change; still tracked as follow-up |
| `.kiro/steering/builds.md` | Process authority: sequential builds, security gate pre-check, portal-deploy sequencing, on-hardware verification | No change; this design complies |

## Deployment and On-Hardware Verification

### Rollout shape and scheduling

1. **One component build at a time** (`pgrep -af "gdk component build"` /
   `pgrep -af "build-custom.sh"` before dispatching anything; builds.md).
2. **Pre-build gate:** no preservation-tracked file changes expected — still
   run the security guard pair and confirm green before any build; move
   `cdk.out` aside; **no portal deploy while any build runs**:
   ```
   python3 -m pytest \
     test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py \
     test/backend-test/security/preservation/test_preservation_secrets_out_of_scope_guard.py \
     -p no:cacheprovider --noconftest -q
   ```
3. **Build order:** JP7 first (`aws.edgeml.dda.LocalServer.arm64JP7`, log to
   `.gdk_build_jp7.log`) — jetson-thor1 is the verification device; JP6 next
   (the other vLLM-capable target), then JP5 when scheduled (vLLM-free —
   inertness smoke only). Where sensible, share the build cycle with other
   pending device-side specs so the fleet takes one version bump.
4. **Portal deploy (File 7) strictly AFTER all component builds finish**,
   then move the fresh `cdk.out` aside before any future build. The portal
   leg is independent of the device leg and can also land first if no build
   is pending — the sequencing constraint is only "never during a build".

### Session A — jetson-thor1 (JP7): device leg (USER ACTION — requires the physical device)

Every step on jetson-thor1 is a USER ACTION: host tests cannot restart real
backends, load real engines, or drive Greengrass lifecycles.

1. **Deploy the JP7 component.** Confirm qwen loads via the normal component
   Startup (READY in `/v2/repository/index` and feature-config), backend
   healthy — the fresh-deploy single-load check: exactly ONE "Loading vLLM
   model" per model in the backend log even though the reconciler also ran.
2. **The core reproduction (2.1):** `sudo docker restart` the backend
   container (or wait for a natural awscrt abort — do NOT induce one). On
   the unfixed tree this orphans the model forever; assert on the fixed
   tree: the reconciler log announces the scan, the model transitions
   LOADING (feature-config + 409 body during the window, 2.2/2.3) and
   reaches READY with NO human action and NO component restart; a generate
   smoke serves; the workflow LLM binding path survives a restart issued
   mid-workflow (poll rides the reload within its 240 s budget — engine
   load time on Thor is ~60 s per the incident record).
3. **Repeat-restart churn:** restart the backend twice in quick succession
   (the Amendment A3 shape); assert convergence to READY once restarts stop,
   with bounded, logged retries in between and no retry storm.
4. **Tombstone (3.5/2.4):** `POST /v2/repository/models/qwen/unload`;
   restart the backend; assert the model is NOT resurrected, reports
   STOPPED (feature-config) and 409 "unloaded" (generate). Restart the model
   COMPONENT (re-stage): assert the tombstone is gone and the model loads.
   Then `--cleanup` leg: remove via a deployment that drops the component;
   assert nothing resurrects and nothing scans.
5. **Terminal-failure truthfulness (2.3):** with the model unloaded, corrupt
   the staged `model.json` (device-side, reversible), restart the backend;
   assert bounded retries then FAILED with the validation reason retained —
   never eternal LOADING. Restore the file; restart; assert recovery.
6. **Sustained health per builds.md:** leave the device under normal
   operation for a sustained period — no crash-loop, no container restart
   regression, no reconciler false activity (idle after its one-shot scan).

### JP6 follow-on (USER ACTION)

Same smoke on a JP6 vLLM-capable device (ryan-orin-nano class): backend
restart → automatic reload → READY; tombstone honored. Per builds.md, the
change is not "done" on an arch until verified there.

### JP5 / x86 inertness smoke (3.6)

On a vLLM-free image: assert startup logs show the pre-feature sequence (no
"vLLM runtime manager started", no reconciler line), no `vllm-reconciler`
thread, backend healthy. Host-side the same is asserted with
`VLLM_AVAILABLE` forced false; the device smoke is the honest confirmation.

### Portal leg verification (after portal deploy)

Re-package a vLLM-referencing workflow for arm64_jp7 in the real account:
assert the new component version's recipe carries ONLY the suffixed
`...-jetson-xavier-jp7` model dependency; package against the legacy
unsuffixed-only record shape (staging/test record): assert the fail-closed
error names the model and architecture. Deploy the re-packaged workflow to
jetson-thor1 and assert the dependency closure contains NO JP6 lineage
(the account remediation — workflow 7.0.1 — already demonstrated the shape;
this verifies the CODE now produces it).

## Testing Strategy

### Validation Approach

Two phases per the bugfix methodology: exploration tests written to assert the
FIXED expectation run first on the UNFIXED tree and FAIL (their
counterexamples prove the bug), then become the fix-check suite; preservation
tests PASS on the unfixed tree and must keep passing. Everything below is
host-runnable and GPU-free — the honesty guard at the end states exactly what
is not. Device-leg suite: `test/backend-test/vllm_model_reload/` (fake engine
factory via the manager's injectable `engine_factory`, temp `VLLM_MODEL_DIR`
trees via `tmp_path`, a real `VllmRuntimeServer` on an ephemeral port where
the reconciler's HTTP path is exercised, FastAPI `TestClient` elsewhere).
Portal-leg suite: `edge-cv-portal/backend/tests/` (moto `aws_stack` conftest
fixture, registry-record fixtures in both modern and legacy shapes).
Hypothesis drives the property tests through the conftest-registered profiles.

### Test commands

- **Device-side suites** run host-side in the portal venv from the repo root
  (the flask-app image is not on this host — the established caveat; the
  container gate runs at build time):
  ```
  source /home/ubuntu/.venvs/dda-portal-tests/bin/activate
  PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
      test/backend-test/vllm_model_reload -q -p no:cacheprovider --noconftest
  ```
  The existing pinned suites (`test/backend-test/vllm_runtime`,
  `vllm_runtime_tests`, `text_generation`, `deploy_reliability`) run the same
  way and must stay green untouched.
- **Portal suites** run from `edge-cv-portal/backend` in the same venv WITH
  conftest (moto `aws_stack` fixture; Hypothesis profiles
  `portal-fast`/`ci` are conftest-registered — do NOT hardcode
  `max_examples`; do NOT use `--noconftest`):
  ```
  python3 -m pytest tests/test_vllm_workflow_arch_dependency_exploration.py \
      tests/test_vllm_workflow_arch_dependency_properties.py \
      tests/test_vllm_workflow_arch_dependency_units.py \
      -q -p no:cacheprovider
  ```
- **Security guard pair** (before any build; command in the Deployment
  section) — expected untouched-green: this spec edits no
  preservation-tracked file.

### Exploratory Bug Condition Checking

**Goal**: surface counterexamples demonstrating both defect legs on UNFIXED
code. All cases FAIL on unfixed code — this confirms the bug.

1. **The orphaned-model core (defect 1.1)**: build a staged repo tree; run
   the "restarted backend" harness (fresh manager + server + — on the fixed
   tree — reconciler) with a recording fake engine factory; assert a load is
   driven to READY. Unfixed: zero factory calls, state stays STAGED forever.
2. **409 forever (defect 1.3)**: same harness; generate against the staged
   model; assert it eventually serves. Unfixed: 409 on every request with no
   load in flight.
3. **Eternal LOADING (defect 1.4)**: `get_features_vllm()` over the
   restarted-backend manager; assert the model does not report LOADING
   indefinitely while no load is in flight (it reaches READY/FAILED).
   Unfixed: "LOADING" on every read, forever.
4. **No reconciliation module exists**: `vllm_runtime.reconciler` imports and
   `start_vllm_runtime` wires it — absent on unfixed code.
5. **Portal leg (defect 1.6)**: registry record in the INCIDENT shape (legacy
   unsuffixed `published_component` for the base name; separately, a modern
   record whose singular map carries the unsuffixed name PLUS suffixed
   `components` entries); resolve for `['arm64_jp7']`; assert the emitted
   dependency set contains only suffixed names. Unfixed: the base name is
   emitted verbatim — the counterexample is the incident's exact HARD
   dependency (`model-vllm-qwen3-vl-8b-instruct >=0.0.0`).

**Expected counterexamples** (documented when the suite runs on unfixed
code): the empty `_models` table + zero load requests after "restart"; the
unbounded 409/"LOADING" pair; the unsuffixed dependency entry.

### Fix Checking

**Goal**: for all inputs where the bug condition holds, the fixed tree
produces the expected behavior.

**Pseudocode:**
```
FOR ALL X WHERE isBugCondition(X) DO
  result := restart_and_reconcile_fixed(X)
  ASSERT loadReissued(result) AND truthfulStatus(result)
         AND boundedRetries(result) AND tombstoneHonored(result)
END FOR
```

**Test cases (the exploration suite above, now passing, PLUS):**
1. **Reconciler lifecycle property (Property 1)**: _for any_ generated set
   of staged repos with per-model factory outcomes (success, permanent
   failure, fail-then-succeed, KV-OOM-marker failure), every desired model
   ends READY or FAILED-with-reason; loads are issued strictly sequentially
   in sorted order; per-model attempt counts never exceed the schedule;
   KV-OOM triggers exactly one unload→reload per attempt.
2. **Reload-window 409 (2.2)**: slow fake factory (event-controlled);
   generate during the window → 409 with `"state": "loading"` category;
   after completion the same request serves; the workflow binding's poll
   loop (invoked with a short test budget) rides through.
3. **Truthful status property (Property 3)**: _for any_ manager/tombstone
   state combination, the `(feature-config status, 409 category)` pair
   matches the truth table: READY→(READY, ready), LOADING/desired-STAGED→
   (LOADING, loading), tombstoned→(STOPPED, unloaded),
   FAILED→(FAILED+reason, failed), UNKNOWN→(absent/unknown).
4. **Tombstone property (Property 4)**: _for any_ operation sequence drawn
   from {unload, load, re-stage, cleanup, restart}, reconciliation reloads
   iff staged-and-re-armed; marker write failure (read-only dir) never fails
   the unload; re-stage (simulated with the REAL `stage_repository()` from
   `vllm_model_prep` against the temp tree) clears the marker.
5. **Fresh-deploy single-load (3.1 interaction)**: component-Startup load
   (HTTP POST, the real prep `request_load` against the ephemeral server)
   racing the reconciler's POST for the same model → exactly ONE engine
   construction (factory call count == 1).
6. **Portal resolution property (Property 5)**: _for any_ generated record
   shape × arch selection, emitted names are always suffixed and cover the
   selection, or `PackagingError` names model + uncovered archs; the base
   name never appears; multi-arch divergence follows Defect F omission.
7. **Arch-contradiction guard**: fake/moto Greengrass recipe fixtures —
   contradiction → `PackagingError` naming component/variant/arch; matching
   variant → pass; no LocalServer dep → pass with warning; `get_component`
   raising → warn and proceed.

### Preservation Checking

**Goal**: for all inputs where the bug condition does NOT hold, fixed
behavior equals unfixed behavior.

**Pseudocode:**
```
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

**Test plan (these PASS on unfixed code):**
1. **`vllm_model_prep.py` unmodified**: hash pin of the file against the
   unfixed tree (3.1, 3.7) — the strongest available statement that
   first-deploy Startup semantics cannot have changed.
2. **Existing suites untouched-green**: `test/backend-test/vllm_runtime*`,
   `text_generation`, `deploy_reliability` (prep classification suites),
   `workflow_engine` LLM-binding tests — run unmodified; they pin 3.1, 3.2,
   3.4 behavior.
3. **Manager state-machine identity (3.2)**: _for any_ load/unload/fail
   sequence WITHOUT tombstones, fixed `state()`/`list_models()` outputs
   deep-equal unfixed outputs (UNLOADED is unreachable without a tombstone;
   executable property with the fake factory).
4. **Unload identity (3.5)**: return values and engine-freeing behavior for
   tracked/untracked/READY/FAILED models identical; only the marker file is
   new.
5. **Status payload identity (3.4)**: _for any_ manager model set without
   tombstones, `get_features_vllm()` output deep-equals unfixed output; the
   `_STATE_CATEGORY`/`_VLLM_STATUS_MAP` additions are pure additions
   (existing keys byte-identical — executable as a dict-subset assertion).
6. **vLLM-free inertness (3.6)**: with `VLLM_AVAILABLE` forced false,
   `start_vllm_runtime()` returns None with no reconciler import executed
   (module absence from `sys.modules` asserted) and no thread named
   `vllm-reconciler`.
7. **Constants/bind identity (3.8)**: `VLLM_RUNTIME_HOST`/`VLLM_RUNTIME_PORT`
   and `VllmRuntimeServer` bind arguments unchanged.
8. **Portal preservation property (3.9)**: _for any_ vision-only,
   plugin-only, or model-free workflow input (generated record shapes and
   arch selections), fixed `resolve_model_components` +
   `model_component_dependencies` output deep-equals a pinned reference of
   the unfixed behavior (Defect F/G, plugin pinning, LocalServer
   single-variant discipline); the existing `test_vllm_multi_arch_publish_*`
   and vision packaging suites stay green untouched.

### Unit Tests

- Tombstone path construction, JSON content tolerance (corrupt marker still
  counts as tombstoned), `_tombstoned` on missing repo.
- Reconciler candidate snapshot: STAGED included, UNLOADED/LOADING/READY/
  FAILED-tracked excluded; empty `VLLM_MODEL_DIR`; dir absent.
- Backoff arithmetic and attempt accounting; KV-OOM marker matching reuses
  the prep markers verbatim.
- `_resolve_vllm_components`: modern/intermediate/legacy record shapes,
  malformed entries (non-dict, blank names) skipped, secondary-source target
  matching uses primary ids only (never `onnx-jetson-xavier-jp7`).
- Guard recipe parsing: multiple LocalServer keys, missing dependencies
  block, malformed recipe JSON → warn-and-proceed.

### Property-Based Tests

- Fix-check 1, 3, 4, 6 and preservation 3, 5, 8 above (Hypothesis;
  conftest-registered profiles, no hardcoded `max_examples`). Generators
  constrain intelligently: staged-repo trees generated from valid layouts ±
  targeted defects; record shapes generated over the real field vocabulary
  (`components`/`published_components`/legacy) rather than arbitrary JSON;
  operation sequences for the tombstone property drawn from the closed
  five-operation alphabet.

### Integration Tests

- Full device-leg pass through real components: temp tree staged by the REAL
  `stage_repository()`, real `VllmRuntimeManager` (fake factory), real
  `VllmRuntimeServer` on an ephemeral port, real `VllmReconciler` — restart
  simulated by tearing all three down and rebuilding over the surviving
  tree; generate + feature-config asserted end to end.
- Portal-leg pass through the moto stack: registry snapshot → resolution →
  dependency emission → recipe assembly, asserting the final
  ComponentDependencies block.
- On-hardware Session A (above) is the real integration tier.

### Honesty Guard — what host tests CANNOT prove (device sessions are USER ACTIONs)

Everything above runs GPU-free with fakes. The following are ONLY provable on
real hardware, and the verification plan assigns each to a user-scheduled
device session:

- **Real engine reload**: that a genuine `AsyncLLMEngine` reconstructs
  cleanly on the post-restart GPU (KV-cache reservation, the validated
  KV-OOM recovery firing for real, engine/event-loop affinity under real
  vLLM) — Session A steps 1–3. Host tests SIMULATE the engine via the
  injectable factory; they prove orchestration, not CUDA.
- **Real backend restarts**: docker/compose recovery, the awscrt-abort
  restart class, and process teardown semantics — Session A step 2/3. The
  host harness models a restart as object reconstruction, which cannot
  capture container lifecycle timing.
- **Real Greengrass lifecycle**: component Startup racing the reconciler
  under an actual deployment, `--cleanup` under a component removal, and
  deployment-churn windows — Session A steps 1 and 4; the fresh-deploy
  single-load claim is only fully proven there.
- **Shadow/IPC propagation of the truthful statuses** to IoT Core and the
  portal — device-only (host tests stop at `get_features_vllm()`; the shadow
  transport is faked/absent host-side).
- **The workflow LLM binding riding a real ~60 s engine reload** inside its
  240 s budget on Thor — Session A step 2.
- **JP6 parity and JP5/x86 inertness on real images** — the follow-on
  smokes; identical Python, but per builds.md not "done" until verified per
  arch.
- **The portal leg in the real account**: real Greengrass recipes for the
  guard, the re-packaged workflow's closure on a real device — the
  post-deploy portal verification above.
