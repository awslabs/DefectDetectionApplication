# Build Server Disk Pruning Bugfix Design

## Overview

Fleet JP7 build `e1d672ce` on the dedicated build server `i-092e45480d30c89c4`
(192 GB disk) died mid-run with `RUNNER_DISK_FULL` (ENOSPC during docker layer
extraction) ~1 hour in, AFTER the expensive GPU compile — with the disk
preflight having "passed" at 29 GB free, because the preflight is
measurement-only (`PREFLIGHT_MIN_DISK_GB` unset in production). The root cause
is unbounded accumulation across builds: every ECR-path publish in
`portal-build.sh` runs `docker tag flask-app:latest ${ECR_REPO}:${VERSION}` +
`docker push` and never removes the local per-version tag, so stale
locally-tagged image generations pile up (~74 GB of `dda/flask-app` +
`dda/react-webapp` `:1.0.x` tags at incident time, all safely in ECR), joined
by old timestamped `/tmp` build logs. The only between-build cleanup is
`portal-build.sh` step [3/7] (`rm -rf greengrass-build/ .gdk/`).

The fix is three-pronged, engineered so that NO portal deploy is needed (the
fix ships to the build server via the repo source sync that precedes every
dispatched build — a git push suffices):

1. **Stop the leak at the source** — `portal-build.sh` untags each local
   per-version ECR ref immediately after its successful `docker push`
   (`docker rmi` of a multi-tagged image only removes the tag; the layers stay
   referenced by `flask-app:latest` / `react-webapp:latest`).
2. **Reclaim stale state when a new build starts** — a NEW standalone script
   `scripts/prune-build-server-disk.sh`, invoked by a small existence-gated
   hook in `scripts/portal-build-agent.sh` after the source sync and inside
   the build lock. It prunes ONLY provably-safe state: locally-tagged
   per-version ECR-repo images whose exact digest is CONFIRMED present in ECR,
   dangling images, stale `custom-build/` staging leftovers, and old
   pattern-matched `/tmp` build logs — logging every action and the sizes
   freed. It never touches `:latest` tags, never touches the docker build
   cache (no `builder prune`, no `system prune`, no `image prune -a`), and
   fails OPEN (skip + loud log) when ECR is unreachable or anything is
   uncertain.
3. **Enforce a minimum free-disk threshold AFTER pruning** — the prune script
   re-measures free space on the docker storage volume and exits with a
   distinct code when it is below `BUILD_MIN_FREE_DISK_GB` (default 60); the
   agent then fails the job BEFORE the expensive build with
   `error_kind=disk`, which the existing backend classification already maps
   to `RUNNER_DISK_FULL` — no `build_dispatcher.py` / backend change, no
   portal deploy.

The keystone economics (Decision 1): the pruning logic lives in a NEW script,
so the two pinned scripts get minimal, contract-preserving diffs. The agent's
`build_save_pkgs` neighbor-script sha256 golden is consciously rebaselined in
the same change; the `portal_builds` suites' pinned contracts (argument
contract, exit codes 64/75/78, preflight markers, phase-event field sets,
ERROR_TAIL derivation) are preserved untouched — verified in this design
against the actual test sources.

A JP7 component build (job `998b6f42`) is RUNNING on the build server. The
git push that ships this fix and all live verification are user-sequenced
tasks that must not interfere with it; the durable-fix verification happens at
the NEXT dispatched fleet build.

## Glossary

- **Bug_Condition (C)**: the state where a new build starts on a dedicated
  build server with stale reclaimable disk state present (locally-tagged
  per-version ECR image generations confirmed in ECR, dangling images, old
  `/tmp` build logs) and/or free disk below the build's real footprint — and
  the unfixed scripts neither prune nor fail fast, proceeding into a build
  that dies mid-run with ENOSPC
- **Property (P)**: the desired behavior — before the build proceeds, stale
  state is pruned (safely: only ECR-confirmed generations, never `:latest`,
  never the build cache), every prune action is logged with sizes freed, and
  the build fails fast with a clear disk error when free space is still below
  the minimum after pruning
- **Preservation**: identical build behavior on plentiful-disk/nothing-stale
  runs (same steps, versions, phase events, `PORTAL_BUILD_RESULT`), ECR
  contents never deleted, in-progress builds untouched, unchanged fleet error
  classification (`RUNNER_DISK_FULL` via `build_reconciliation`), unchanged
  local dev builds, and the pinned script-contract test suites
- **Per-version locally-tagged generation**: a local docker tag of the form
  `<acct>.dkr.ecr.<region>.amazonaws.com/dda/flask-app:<M.m.p>` (same for
  `dda/react-webapp`) left behind by the ECR publish path; ~29–42 GB each
  observed
- **`:latest` lineage**: `edgemlsdk:latest`, `flask-app:latest`,
  `react-webapp:latest` — the unqualified local tags `build-custom.sh`
  produces and docker-compose consumes; they carry the onnxruntime GPU
  compile layers and MUST survive pruning
- **Docker build cache**: BuildKit cache on the server (snap docker,
  storage under `/var/snap/docker/common`); evicting it forces the full
  ~1–2 h GPU onnxruntime recompile — pruning must never touch it
- **RepoDigest**: the `repo@sha256:...` entry docker records on a local image
  after a successful push/pull; its presence for the ECR repo proves that
  exact content was pushed
- **ECR-confirmed**: the prune safety gate — the local image's RepoDigest for
  the ECR repo exists AND `aws ecr describe-images` with that `imageDigest`
  succeeds (the exact bytes are retrievable from ECR)
- **Fail open**: on ECR unreachability/auth failure/any uncertainty, the
  prune script RETAINS the image (skips deletion), logs loudly, and lets the
  build proceed — a prune malfunction must never delete an unpushed image and
  must never block a build on its own
- **`BUILD_MIN_FREE_DISK_GB`**: env knob read by the prune script (default
  60): minimum free GB required on the docker storage volume AFTER pruning;
  `0` disables enforcement
- **`PREFLIGHT_MIN_DISK_GB`**: the EXISTING agent preflight knob — remains
  unset/measurement-only; this fix does not change the preflight contract
  (the `portal_builds` preflight suites pin it)
- **Build lock**: `/var/lock/dda-build.lock`, `flock`ed by the agent for its
  lifetime — pruning runs inside it, so it can never race an in-progress
  build on the server
- **Agent sandbox tests**: `test_source_selection_preservation.py` runs a
  byte-identical copy of the agent in a temp repo containing ONLY
  `scripts/portal-build-agent.sh` + a stub `portal-build.sh`; the prune hook
  is existence-gated so those tests stay valid without modification
- **Neighbor-script sha256 golden**: `test/backend-test/build_save_pkgs/`
  pins `scripts/portal-build-agent.sh` bit-identical
  (`baselines/scripts_portal-build-agent.sh.sha256.txt`) — an intended agent
  edit requires rebaselining that one hash, never weakening the test
- **`error_kind=disk`**: the agent terminal-callback shortcut
  (`build_reconciliation.AGENT_ERROR_KIND_DISK`) that classifies the job
  `RUNNER_DISK_FULL` directly — reused for the pre-build threshold failure so
  no backend change is needed

## Bug Details

### Bug Condition

The bug manifests when a build is dispatched to a dedicated build server whose
disk carries stale reclaimable state from previous builds and/or less free
space than the build's real footprint. The unfixed scripts (a) leave a new
per-version generation behind on every ECR publish, (b) perform no
between-build pruning beyond `rm -rf greengrass-build/ .gdk/`, and (c) enforce
no minimum-free threshold — so the build proceeds, spends the ~1 h GPU
compile, and dies mid-extraction with ENOSPC, requiring manual operator
cleanup and a requeue.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X of type BuildStart
         { staleGenerations: list of LocalImageTag,   // per-version ECR-repo tags, ECR-confirmed
           staleTmpLogs: list of File,                // gdk-build-*, gdk-publish-*, portal-build-agent-*, inference-uploader-build-*
           danglingImages: list of Image,
           freeDiskGB: int,                           // docker storage volume
           buildFootprintGB: int }                    // real footprint of the dispatched target
  OUTPUT: boolean

  // The unfixed tree neither prunes nor fails fast:
  RETURN (X.staleGenerations ≠ ∅ OR X.staleTmpLogs ≠ ∅ OR X.danglingImages ≠ ∅
              // reclaimable state exists but no prune runs (defects 1.1, 1.2)
          )
         OR (X.freeDiskGB < X.buildFootprintGB
              // the build proceeds anyway and dies mid-run (defects 1.3, 1.4, 1.5)
          )
END FUNCTION
```

On the unfixed tree every dispatched build after an ECR-path publish satisfies
C(X): each publish adds two per-version generations that nothing removes, and
the preflight passes on any nonzero free space (`PREFLIGHT_MIN_DISK_GB`
unset).

### Examples

- **Job `e1d672ce` (the motivating incident)**: 8 stale generations (~74 GB,
  all in ECR) + old `/tmp` logs accumulated; preflight recorded 29 GB free
  and PASSED (measurement-only); the JP7 build ran ~1 h, then died with
  `no space left on device` extracting `libonnxruntime_providers_cuda.so`;
  the agent's `tee` to `/tmp` also hit ENOSPC; recovery was manual deletion
  (54 → 128 GB free) plus a requeue (job `998b6f42`). Expected: the stale
  74 GB is pruned before the build starts, and if free space were still
  below the minimum, the job fails in seconds with a clear disk error.
- **Every ECR-path publish (defect 1.1)**: `docker tag flask-app:latest
  ${ECR_REPO_BACKEND}:${COMPONENT_VERSION}` + push, never untagged — one new
  ~29–42 GB generation per publish per image. Expected (fixed
  `portal-build.sh`): the per-version ref is untagged immediately after its
  successful push; layers remain via `:latest`.
- **Unpushed image safety (must NOT be deleted)**: a publish whose `docker
  push` failed leaves a local per-version tag NOT in ECR. Expected: the prune
  script retains it (no RepoDigest / no ECR confirmation) and logs the
  retention loudly. Never delete anything not provably in ECR.
- **ECR unreachable (fail open)**: `aws ecr describe-images` errors (network,
  creds). Expected: zero image deletions of unverifiable candidates, a loud
  log line, the build proceeds (the threshold check still runs on whatever
  was freed by the always-safe steps).
- **Cache-retention property (the manual remediation's deliberate KEEP)**:
  `edgemlsdk:latest` / `flask-app:latest` / `react-webapp:latest` and the
  BuildKit cache carry the onnxruntime GPU compile layers. Expected: pruning
  never targets any `:latest` tag, any unqualified local repo, or the builder
  cache — a blanket `docker system prune -af` is exactly what the fix must
  NOT do.
- **Healthy server (must not change, 3.1)**: plentiful disk, nothing stale.
  Expected: the prune script no-ops (zero deletions), logs the measurement,
  and the build proceeds byte-identically to today.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Plentiful-disk builds are byte-equivalent (3.1): same steps and ordering,
  same phase events and detail field sets, same `PORTAL_BUILD_RESULT` line,
  same published artifacts and versions.
- ECR contents (3.2): pruning acts ONLY on local tags; nothing ever deletes
  from ECR (`describe-images` is the only ECR call the prune script makes).
- In-progress builds (3.3): the prune hook runs inside
  `/var/lock/dda-build.lock` after the agent acquires it, so exactly one
  build's prune can run and never concurrently with another build on the
  server; the current job's own log and the freshly-synced workspace are
  excluded from deletion.
- Fleet error classification (3.4): genuine mid-build ENOSPC still flows
  through the existing `error_kind=disk` / ENOSPC-evidence paths to
  `RUNNER_DISK_FULL`; the new pre-build threshold failure REUSES
  `error_kind=disk` (same detail field set as every `emit_failed`).
- Local dev builds (3.5): `run_jp_builds.sh` / direct `gdk component build`
  never invoke the agent or `portal-build.sh`; the prune script is not shared
  with them.
- Pinned script contracts (3.6): agent argument contract, exit codes
  64/75/78, preflight markers and field sets, ERROR_TAIL derivation, target
  mapping — all preserved verbatim (verified against
  `test_source_selection_preservation.py`,
  `test_agent_tail_truncation_properties.py`,
  `test_preflight_target_matrix_properties.py`,
  `test_execution_failure_preservation.py`,
  `test_preflight_agent_contract.py`). The ONE intended golden update is the
  `build_save_pkgs` neighbor-script sha256 for the agent.

**Scope:**
All inputs that do NOT involve stale reclaimable state or a below-threshold
disk are completely unaffected. This includes:
- Builds on a freshly-pruned or roomy server (prune no-ops, threshold passes)
- The under-2GB GDK publish path (no per-version ECR tags are created there)
- Ephemeral runners (fresh disks; the hook is harmless — nothing stale to
  prune, threshold trivially passes on a right-sized volume)
- Manual `portal-build.sh` runs (they gain the post-push untag only; no
  pruning, since the hook lives in the agent)
- `publish-ecr-only.sh` (untouched — pinned by its own sha256 golden; the one
  local tag a manual republish leaves is reclaimed by the next dispatched
  build's prune)

## Hypothesized Root Cause

> Not a hypothesis: the incident investigation completed before requirements
> were written (bugfix.md Introduction). Section header kept per the bugfix
> design format. Stated for the record:

1. **Publish-path tag leak (defect 1.1)**: the ECR path in `portal-build.sh`
   tags `flask-app:latest` / `react-webapp:latest` with the per-version ECR
   ref and pushes, but no untag follows — by design of the original >2GB
   re-integration, which simply never considered local tag lifetime.
2. **No between-build pruning (defect 1.2)**: the only cleanup is
   `portal-build.sh` step [3/7] `rm -rf greengrass-build/ .gdk/`
   (`build-custom.sh` additionally does `rm -rf ./custom-build` at its own
   start). Image generations, dangling images, and `/tmp` logs are nobody's
   job.
3. **Measurement-only preflight (defect 1.3)**: `record_disk_capacity` in the
   agent records evidence; `PREFLIGHT_MIN_DISK_GB` is unset in production, so
   the threshold branch never fires — 29 GB free passed.
4. **Consequence (defects 1.4, 1.5)**: nothing stands between accumulated
   consumption and a mid-build ENOSPC ~1 h in; recovery is manual.

## Design Decisions

The open questions from bugfix.md, investigated and decided.

### Decision 1 — Pruning lives in a NEW standalone script; the agent gets an existence-gated hook (inside the lock, after the source sync)

**Decision:** create `scripts/prune-build-server-disk.sh` (new file, not
pinned by any golden). `scripts/portal-build-agent.sh` invokes it via a small
hook placed BETWEEN Step 2 (source sync) and Step 3 (phase=building event):

```bash
# ── Step 2.5: prune stale disk state from previous builds ────────────────
PRUNE_SCRIPT="${REPO_DIR}/scripts/prune-build-server-disk.sh"
if [ -f "$PRUNE_SCRIPT" ]; then
    echo "Running pre-build disk prune (${PRUNE_SCRIPT})..."
    bash "$PRUNE_SCRIPT" BUILD_JOB_ID="$BUILD_JOB_ID"
    PRUNE_EXIT_CODE=$?
    if [ "$PRUNE_EXIT_CODE" -eq 3 ]; then
        # Below the minimum AFTER pruning: fail BEFORE the expensive build.
        emit_failed "disk" "Insufficient disk space before build: free space below BUILD_MIN_FREE_DISK_GB after pruning (see prune log above for measured capacity)"
        exit 1
    elif [ "$PRUNE_EXIT_CODE" -ne 0 ]; then
        echo "⚠ Warning: disk prune exited ${PRUNE_EXIT_CODE} — continuing (fail-open); disk state may be unpruned"
    fi
else
    echo "ℹ No prune script at ${PRUNE_SCRIPT} — skipping pre-build disk prune"
fi
```

**Rationale:**
- **Minimal diff to the sha-pinned agent.** The agent is pinned bit-identical
  by the `build_save_pkgs` neighbor golden; a ~15-line hook is a small,
  reviewable, intended rebaseline. All the pruning complexity (and its test
  surface) lives in the new, unpinned script.
- **The sandbox tests stay valid unmodified.** The
  `test_source_selection_preservation.py` sandbox copies ONLY the agent + a
  stub `portal-build.sh`; the existence gate makes the hook a no-op there
  (one info line on stdout, which the detail parser ignores). Exit codes
  64/75 and every pinned detail field set are reached before/without the
  hook or are unchanged by it.
- **Placement after the sync guarantees version consistency**: the prune
  script that runs is the one belonging to the job's `source_ref` (the
  dispatcher's preamble sync already places the clone on the ref before the
  agent starts, so the script exists on first post-push dispatch).
- **Placement inside the lock satisfies 3.3 by construction**: the server
  runs at most one agent at a time; pruning can never race a build.
- **Runs for legacy and ATTEMPT_ID dispatches alike** — "when a new build
  starts" is unconditional, unlike the ATTEMPT_ID-gated preflight.
- **Exit-code contract:** prune exit 0 = proceed; exit 3 = below threshold →
  agent emits `error_kind=disk` (classified `RUNNER_DISK_FULL` by the
  existing backend, no deploy) and exits 1 (a plain build-failure exit —
  the pinned 64/75/78 codes keep their exact meanings); any other nonzero =
  prune malfunction → loud warning, build proceeds (fail open).
- **Rejected — hook in `portal-build.sh` step [3/7]:** also runs on manual
  invocations outside the lock and outside the fleet lifecycle; the agent is
  the dedicated-server "new build starts" boundary and owns the job-failure
  channel (`emit_failed`).
- **Rejected — enforcing via the existing preflight (`PREFLIGHT_MIN_DISK_GB`
  default):** the preflight runs BEFORE pruning could fix the disk, so a
  reclaimable server would fail needlessly (requirements 2.3 allows "prune
  then re-check", which is strictly better); it is also ATTEMPT_ID-gated and
  its behavior is pinned by the preflight suites. The preflight stays
  measurement-only and byte-untouched.

### Decision 2 — ECR-safety mechanism: digest-confirmed untag, fail open on any uncertainty

**Decision:** a local per-version tag is deleted ONLY when ALL of:
1. its repository matches the ECR registry pattern
   `*.dkr.ecr.*.amazonaws.com/dda/*` (unqualified repos like `flask-app`,
   `edgemlsdk` can never match);
2. its tag is not `latest` (and not `<none>` — dangling is handled
   separately);
3. the local image records a RepoDigest for that ECR repository (proof a push
   of these exact bytes completed);
4. `aws ecr describe-images --repository-name <repo> --image-ids
   imageDigest=<that digest>` SUCCEEDS (the bytes are retrievable from ECR
   right now).

Deletion is `docker rmi <repo>:<tag>` (never `-f`, never by image ID) — on a
multi-tagged image this only removes the tag; layers stay referenced by
`:latest`. Every deletion logs the ref, digest, and reported size; every
retention logs the reason (`NOT_IN_ECR`, `ECR_UNVERIFIABLE`, `NO_REPODIGEST`).
If ANY step of the confirmation errs (aws CLI failure, missing digest,
unparseable output), the image is RETAINED and the script continues — fail
open, loudly.

**Rationale:**
- Confirming by **digest** (not tag) is immune to ECR-side tag mutation and
  to local/remote tag skew: it proves the exact content is in ECR, which is
  requirement 3.2's "safely stored" in its strongest form.
- The four-condition conjunction makes "never delete an unpushed image"
  structural: a failed push leaves no ECR-repo RepoDigest, so conditions 3–4
  can never pass. The pathological repeated-push-failure case accumulates
  retained-and-logged tags rather than risking data loss — an explicit,
  logged operator decision, not silent growth.
- `docker rmi` semantics give the cache-retention property (2.2) for the
  newest generation automatically: the most recent per-version tag usually
  aliases the same image as `:latest`, and untagging it leaves the image
  (and its layers) fully intact.
- **Never** `docker system prune`, `docker builder prune`, or
  `docker image prune -a`: the BuildKit cache and `:latest` lineage carry the
  ~1–2 h onnxruntime GPU compile layers (the manual remediation's deliberate
  KEEP). Dangling-only `docker image prune -f` is safe: it removes untagged,
  unreferenced images and does not touch the BuildKit cache store.

### Decision 3 — Threshold: `BUILD_MIN_FREE_DISK_GB` default 60, enforced post-prune, one number for all targets

**Decision:** after pruning, the script measures free GB on the docker
storage volume (`/var/snap/docker/common` when present, else the repo volume
— the same resolution `record_disk_capacity` uses) and exits 3 when it is
below `BUILD_MIN_FREE_DISK_GB` (env-overridable, default **60**; `0`
disables). The measured values (before/after pruning, freed totals) are
logged either way.

**Rationale (sizing from observed evidence):**
- 29 GB free demonstrably FAILS a JP7 build (job `e1d672ce` died
  mid-extraction), so the floor must be well above 29.
- A single backend image generation is 29–42 GB; the build must hold the
  newly-built layers plus extraction working space plus the `docker save`
  tar staging in `custom-build/` — 60 GB covers the largest observed
  generation (42 GB) with headroom for logs and temp files.
- It must not false-fail a healthy pruned server: post-remediation kept-state
  (`:latest` set + build cache) was ~64 GB of the 192 GB disk → ~128 GB free;
  after a further build's cache growth, ~80–100 GB free is realistic — 60
  passes with margin, 80 would sail close. 60 is the conservative default;
  the env knob covers resizing without a code change (per-target values add
  matrix complexity for no present need — JP7 is the largest footprint and
  sets the number).
- Enforcement deliberately lives in the prune script (post-prune re-check),
  NOT in the agent preflight — see Decision 1. `PREFLIGHT_MIN_DISK_GB` keeps
  its unset/measurement-only production semantics.

### Decision 4 — Root-cause stop in `portal-build.sh`: untag immediately after each successful push

**Decision:** in the ECR publish path, immediately after each successful
`docker push`, add:

```bash
docker rmi "${ECR_REPO_BACKEND}:${COMPONENT_VERSION}" >/dev/null 2>&1 || true
echo "✓ Untagged local ${ECR_REPO_BACKEND}:${COMPONENT_VERSION} (image remains as flask-app:latest and in ECR)"
```

(and the react-webapp equivalent). Non-fatal (`|| true`): an untag failure
must never fail a publish that already succeeded.

**Rationale:** stops NEW accumulation at the source so the prune script's
docker section becomes a backstop for historical state, failed-push leftovers
on other paths, and manual `publish-ecr-only.sh` runs. The local tag has no
consumer after the push: the recipe references the ECR ref
(`docker:<ecr>:<version>`), device-side Install retags from ECR, and the
`SKIP_BUILD=1` republish path re-creates the tag from `flask-app:latest`
anyway. The agent's publish-failure reconstruction greps (`Pushing flask-app
to ECR`, `Creating component version via API`) and `PUSHED_IMAGE_REFS`
metadata are untouched. `portal-build.sh` has NO sha256 golden (verified:
`build_save_pkgs` pins `build-custom.sh` textually and four neighbor scripts
by hash — `portal-build.sh` is not among them), so no rebaseline is needed
for this edit; the `test_preflight_agent_contract.py` textual anchors on
`portal-build.sh` (ATTEMPT_ID handling) are unaffected.

### Decision 5 — `/tmp` logs and workspace leftovers: pattern-scoped, age-based, current job excluded

**Decision:** the prune script deletes `/tmp` files matching EXACTLY the four
build-log patterns (`gdk-build-*.log`, `gdk-publish-*.log`,
`portal-build-agent-*.log`, `inference-uploader-build-*.log`) with mtime
older than `PRUNE_LOG_RETENTION_DAYS` (default 7), always excluding the
current job's log (`portal-build-agent-${BUILD_JOB_ID}.log`). It also removes
stale `custom-build/` contents (previous builds' staging dirs, zips, and any
leftover image tars — worth tens of GB after a failed build), mirroring the
`rm -rf ./custom-build` that `build-custom.sh` itself performs at build start
(so the deletion is provably safe: the very next step would do it anyway,
just after the threshold check instead of before). `greengrass-build/` /
`.gdk/` remain `portal-build.sh` step [3/7]'s job — no duplication.

**Rationale:** the four glob patterns are timestamped/job-suffixed and never
reused (bugfix.md); age-based retention keeps recent logs for incident
forensics (the `e1d672ce` postmortem used them). Deleting `custom-build/`
early makes the threshold measurement honest — otherwise the check could
fail on a server that `build-custom.sh` would have cleaned up seconds later.

### Decision 6 — `build_dispatcher.py` is NOT changed

**Decision:** no dispatcher/backend change; the optional follow-on (e.g., a
dispatcher-set `BUILD_MIN_FREE_DISK_GB` per target, or surfacing prune
metrics in events) is explicitly deferred.

**Rationale:** the agent-side env default plus the existing
`error_kind=disk → RUNNER_DISK_FULL` classification deliver the full
requirement set with a git push alone; a dispatcher edit would force a portal
deploy, which builds.md forbids while a component build is running and which
the requirements scope marks optional. Nothing in the dispatcher pins the
agent's stdout, and the SendCommand contract (script path + KEY=VALUE args)
is untouched.

## Correctness Properties

Property 1: Bug Condition - New Builds Prune Stale Disk State and Fail Fast on Insufficient Disk

_For any_ build start where the bug condition holds (isBugCondition returns
true — stale ECR-confirmed image generations, old `/tmp` build logs, or
dangling images present, and/or free disk below the minimum), the fixed
scripts SHALL, BEFORE the build proceeds: prune the stale generations (only
those confirmed in ECR), old pattern-matched `/tmp` logs, stale
`custom-build/` leftovers, and dangling images, logging each action and the
sizes freed; untag per-version ECR refs after each successful push so no new
generation is left behind; and, when free space is still below
`BUILD_MIN_FREE_DISK_GB` after pruning, fail the job with a clear
`error_kind=disk` message BEFORE any build work, never proceeding into the
expensive compile.

**Validates: Requirements 2.1, 2.3, 2.4, 2.5**

Property 2: Preservation - Everything Outside the Stale-Disk Surface Is Unchanged

_For any_ input where the bug condition does NOT hold (plentiful disk,
nothing stale), the fixed scripts SHALL produce the same result as the
original scripts: identical build steps, phase-event field sets, exit-code
contract (64/75/78 meanings untouched), `PORTAL_BUILD_RESULT` output, and
published artifacts; ECR contents never deleted; in-progress builds and their
logs/workspaces untouched (prune runs only under the build lock, current-job
log excluded); ENOSPC classification to `RUNNER_DISK_FULL` unchanged; local
dev builds unaffected; and the pinned script-contract suites
(`portal_builds`, `build_save_pkgs`) still pass with exactly ONE intended
golden update — the agent's neighbor-script sha256.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

Property 3: Fix Checking - Prune Safety (ECR-Confirmed Deletions Only, Cache Retained)

_For any_ generated local image inventory (arbitrary repositories, tags,
digests, sizes) and any ECR state (subsets present/absent, digest matches and
mismatches, transient errors), the prune script SHALL delete exactly the set
of tags satisfying ALL of {ECR-registry `dda/*` repository, tag ≠ latest,
local RepoDigest present for that repo, digest confirmed in ECR} and nothing
else; SHALL never invoke `docker rmi -f`, deletion by image ID,
`docker system prune`, `docker builder prune`, or `docker image prune -a`;
SHALL never delete any `:latest` tag or any unqualified-repo image
(`flask-app`, `react-webapp`, `edgemlsdk`); and on ANY ECR/docker query
failure SHALL retain the affected candidates (zero deletions of
unverifiable images), log the retention loudly, and exit without blocking
the build.

**Validates: Requirements 2.1, 2.2, 3.2, 3.3**

Property 4: Fix Checking - Post-Prune Threshold Gate

_For any_ measured free-space value and any `BUILD_MIN_FREE_DISK_GB` setting,
the prune script SHALL exit 3 if and only if enforcement is enabled
(threshold > 0) and the post-prune free space on the docker storage volume is
strictly below the threshold, and exit 0 otherwise; and the fixed agent SHALL,
on prune exit 3, emit a failed event with `error_kind=disk` and a message
identifying the disk insufficiency WITHOUT running `portal-build.sh`, on
prune exit 0 proceed normally, and on any other prune exit code log a
warning and proceed (fail open).

**Validates: Requirements 2.3, 2.4**

Property 5: Fix Checking - Log/Workspace Pruning Is Pattern-Scoped, Age-Gated, and Fully Logged

_For any_ generated `/tmp` population (files of arbitrary names and ages),
the prune script SHALL delete exactly the files matching the four build-log
glob patterns whose age exceeds the retention window, SHALL never delete the
current job's log or any non-matching file, and SHALL emit a log line for
every prune action (images, logs, workspaces, dangling) including the sizes
freed and the before/after free-space measurements.

**Validates: Requirements 2.1, 2.5**

## Fix Implementation

### Changes Required

**File 1 — `scripts/prune-build-server-disk.sh` (NEW; all pruning + threshold logic)**

Standalone bash script; `set -u -o pipefail` but NOT `set -e` (resilient,
per-step error handling — its own bugs must never abort a build). All
external binaries (`docker`, `aws`, `df`, `find`, `stat`) resolved from PATH
so the test suites can stub them. Accepts `KEY=VALUE` args (agent
convention): `BUILD_JOB_ID=<id>`. Env knobs, all `${VAR:-default}`:
`BUILD_MIN_FREE_DISK_GB` (60), `PRUNE_LOG_RETENTION_DAYS` (7),
`DDA_PRUNE_DISABLE` (unset; `1` = log and exit 0 immediately — escape
hatch). Structure:

1. `free_gb_of <path>` + docker-storage-path resolution
   (`/var/snap/docker/common` when present, else the repo volume — mirrors
   `record_disk_capacity`); log the BEFORE measurement.
2. **Stale generation pruning**: enumerate
   `docker image ls --format '{{.Repository}}\t{{.Tag}}\t{{.Size}}'`;
   candidates = repository matches `[0-9]*.dkr.ecr.*.amazonaws.com/dda/*`
   AND tag ∉ {latest, <none>}. For each: read the local RepoDigest for that
   repository from `docker image inspect`; confirm via `aws ecr
   describe-images --repository-name dda/<name> --image-ids
   imageDigest=<digest>`; on full confirmation `docker rmi <repo>:<tag>`
   and log `PRUNED image <ref> (<size>, digest <sha>)`; otherwise log
   `RETAINED image <ref> reason=<NOT_IN_ECR|NO_REPODIGEST|ECR_UNVERIFIABLE>`.
   Any aws/docker error → retain + continue (fail open).
3. **Dangling images**: `docker image prune -f` (dangling only), log its
   reclaimed-space report. NEVER `-a`, NEVER `builder`/`system` prune.
4. **Workspace leftovers**: `rm -rf <repo>/custom-build/` (size logged
   first) — provably safe, `build-custom.sh` does the same at its start.
5. **Old `/tmp` logs**: `find /tmp -maxdepth 1 -name '<pattern>' -mtime
   +${PRUNE_LOG_RETENTION_DAYS}` over the four patterns, excluding
   `portal-build-agent-${BUILD_JOB_ID}.log`; log each deletion with size.
6. **Threshold gate**: log the AFTER measurement and total freed; if
   `BUILD_MIN_FREE_DISK_GB > 0` and free < threshold → log
   `PRUNE-DISK-INSUFFICIENT free=<X>GB required=<Y>GB` and `exit 3`; else
   `exit 0`. Internal unexpected failures exit 4 (agent fails open).

**File 2 — `scripts/portal-build-agent.sh` (edit: the Step 2.5 hook from Decision 1)**

Inserted between Step 2 (source sync) and Step 3 (phase=building). Exactly
the hook shown in Decision 1: existence-gated, exit-3 → `emit_failed "disk"
...` + `exit 1`, other nonzero → warning + proceed. No other line changes.
**Same change set**: rebaseline
`test/backend-test/build_save_pkgs/baselines/scripts_portal-build-agent.sh.sha256.txt`
to the new file hash (the one intended golden update; never weaken the
test).

**File 3 — `portal-build.sh` (edit: post-push untag, Decision 4)**

Two `docker rmi ... || true` + log lines, one after each successful
`docker push` in the ECR path. No golden pins this file; the phase-event,
result-line, and log-marker contracts are untouched.

**Files NOT changed**: `edge-cv-portal/backend/functions/build_dispatcher.py`
(Decision 6), `publish-ecr-only.sh` (pinned; covered by the prune backstop),
`build-custom.sh`, `run_jp_builds.sh`, all backend classification code, all
recipes.

## Testing Strategy

### Validation Approach

Two-phase: first surface counterexamples demonstrating the bug on the UNFIXED
tree (exploration), then verify the fix and preservation. **Honesty guard:**
no test in this spec runs real docker, aws, gdk, or touches the build server.
Script behavioral tests run the REAL shell scripts via `subprocess.run` with
stub binaries on PATH (`docker`, `aws`, `df`, `find` recording their argv and
returning canned outputs — the `deploy_reliability` /
`csi_nvargus_optional` stub-binary pattern) inside sandbox repos (the
`test_source_selection_preservation.py` `_sandbox_repo` technique). The real
claims — actual space reclaimed on `i-092e45480d30c89c4`, real ECR
confirmation, a real fleet build passing the threshold — are ONLY provable on
the build server and are assigned to the USER ACTION tasks, at the NEXT
dispatched build (job `998b6f42` is running now and must not be disturbed).

Host-side suites run in the portal venv:

```
source /home/ubuntu/.venvs/dda-portal-tests/bin/activate
PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
    test/backend-test/build_server_disk_pruning -q -p no:cacheprovider --noconftest
```

Hypothesis PBTs use `test_property_*.py` naming, no hardcoded
`max_examples`, and `# Validates: Requirements …` comments.

### Exploratory Bug Condition Checking

**Goal**: surface counterexamples demonstrating defects 1.1–1.5 on the
UNFIXED tree. Confirm the root-cause record; every check is host-runnable.

**Test Plan**: text fingerprints on the three scripts plus sandbox-agent
behavioral legs. Run on the UNFIXED tree — cases 1–4 FAIL (expected).

**Test Cases**:
1. **Publish-path untag exists (defect 1.1)**: `portal-build.sh` contains a
   `docker rmi` of `${ECR_REPO_BACKEND}:${COMPONENT_VERSION}` /
   `${ECR_REPO_FRONTEND}:${COMPONENT_VERSION}` after the pushes (text scan,
   comment/string-aware). FAILS on unfixed code (tag+push with no untag).
2. **Prune script exists with the safety anchors (defect 1.2)**:
   `scripts/prune-build-server-disk.sh` exists; contains the ECR
   digest-confirmation call (`describe-images` + `imageDigest`), the
   retention-reason log tokens, the four `/tmp` log patterns, the
   `BUILD_MIN_FREE_DISK_GB` gate, and NO `system prune` / `builder prune` /
   `image prune -a` / `rmi -f`. FAILS on unfixed code (file absent).
3. **Agent invokes the prune before the build (defects 1.2/1.4), behavioral**:
   sandbox repo (real agent copy, stub `portal-build.sh`, recording stub
   prune script planted, `EVENT_BUS` unset) — assert the prune stub ran
   BEFORE the `portal-build.sh` stub. FAILS on unfixed code (never invoked).
4. **Below-threshold fail-fast (defects 1.3/1.4), behavioral**: sandbox with
   a stub prune script exiting 3 — assert the agent emits a `failed` detail
   with `error_kind":"disk"`, does NOT run the `portal-build.sh` stub, and
   exits nonzero. FAILS on unfixed code (the build runs regardless).
5. **F(X) pins — PASS on unfixed, must NOT be inverted**: the agent preflight
   remains measurement-only (`PREFLIGHT_MIN_DISK_GB="${PREFLIGHT_MIN_DISK_GB:-}"`
   with no hardcoded default) and `record_disk_capacity` still exists —
   the preserved preflight contract Decision 3 relies on; step [3/7]
   `rm -rf greengrass-build/` and `build-custom.sh`'s `rm -rf ./custom-build`
   still exist (the existing-cleanup baseline).

**Expected Counterexamples**:
- The unfixed ECR path: `docker tag` + `docker push` × 2 with zero `rmi` —
  one new generation per image per publish
- The unfixed agent: lock → preflight (evidence-only) → sync → building →
  build, with no prune invocation anywhere
- Possible causes: confirmed directly (see Hypothesized Root Cause — no
  re-hypothesis expected)

### Fix Checking

**Goal**: verify that for all inputs where the bug condition holds, the fixed
scripts produce the expected behavior.

**Pseudocode:**
```
FOR ALL X WHERE isBugCondition(X) DO
  result := agent_with_prune(X)          // stub docker/aws/df sandbox
  ASSERT prunedExactlyTheSafeSet(result)          // Property 3
  ASSERT thresholdGateCorrect(result)             // Property 4
  ASSERT everyActionLoggedWithSizes(result)       // Property 5
  ASSERT noBuildWorkWhenBelowThreshold(result)    // Property 1
END FOR
```

### Preservation Checking

**Goal**: verify that for all inputs where the bug condition does NOT hold,
the fixed scripts behave identically to the originals.

**Pseudocode:**
```
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT agent_original(X) = agent_fixed(X)       // events, exit codes, ordering
  ASSERT ecr_state_unchanged(X)                   // describe-images is the only ECR call
END FOR
```

**Testing Approach**: property-based testing for the never-delete guarantees
(they are universal: "for all inventories, nothing outside the safe set is
deleted") and for the no-op identity on clean servers; the pinned-contract
preservation is delegated to the EXISTING suites re-run green
(`portal_builds` agent-contract files, `build_save_pkgs`) with the one
recorded golden rebaseline.

**Test Plan**: observe on UNFIXED code first — record the sandbox agent's
event sequence/exit codes for a clean run, and baseline the existing suites
green — then assert the fixed tree reproduces them.

**Test Cases**:
1. **Clean-server identity**: sandbox agent run with stub docker reporting
   only `:latest` images and roomy stub `df` — same emitted detail sequence
   and exit code as the unfixed baseline; prune section reports zero
   deletions.
2. **Existing-suite green**: `build_save_pkgs` (with the rebaselined agent
   hash) and the `portal_builds` agent-contract suites
   (`test_source_selection_preservation.py`,
   `test_agent_tail_truncation_properties.py`,
   `test_preflight_target_matrix_properties.py`,
   `test_preflight_agent_contract.py`,
   `test_execution_failure_preservation.py`,
   `test_enospc_classification_properties.py`) pass on the fixed tree.
3. **ECR read-only**: for any generated scenario, the recorded `aws` argv
   transcript contains ONLY `ecr describe-images` calls — no delete/put/batch
   ECR mutation ever.

### Unit Tests

- Prune-script candidate classification (registry-pattern matching, latest/
  `<none>` exclusion) over table-driven `docker image ls` outputs
- Agent hook branches: script absent / exit 0 / exit 3 / exit 4
- `portal-build.sh` untag ordering (tag → push → rmi per repo), by text scan
  plus a stub-docker run of the extracted push block

### Property-Based Tests

- `test_property_prune_safety.py` (Property 3): generated image inventories ×
  ECR states → deleted set equals the safe set exactly; fail-open on errors;
  no forbidden docker subcommand ever invoked
- `test_property_prune_threshold.py` (Property 4): generated free-space
  values × thresholds → exit 3 iff enforcement on and free < threshold
- `test_property_prune_logs.py` (Property 5): generated `/tmp` populations →
  deletions exactly the age-gated pattern matches, current-job log immune,
  every action logged

### Integration Tests

- Sandbox end-to-end: real agent + real prune script + stub docker/aws/df +
  stub `portal-build.sh`, over the four scenario classes (stale+roomy,
  stale+tight, clean+roomy, ECR-down) asserting the full event/exit/transcript
  contract
- USER ACTION (real build server, next dispatched fleet build): prune log
  lines and sizes freed visible in the CloudWatch build log, threshold
  measurement logged, build succeeds, ECR images intact, `:latest` lineage
  and build-cache incremental speed preserved (no forced onnxruntime
  recompile)
