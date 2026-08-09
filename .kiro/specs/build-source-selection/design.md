# Build Source Selection Design

## Overview

Portal builds cannot run at all right now, and the reason is a bootstrap
ordering problem that source selection is the fix for. Every bootstrap path
runs a bare `git clone <repo_url>` with no ref, so runners get
`origin/main`. `scripts/portal-build-agent.sh` was added in commit `479ab7f`
and exists only on `origin/feature/portal-build-fleet-and-workflow-gates`.
The agent is therefore invoked from a tree that cannot contain it — SSM
`e9281bdc` (job `1ce014a3`, JP5 ephemeral) and `d75f1ea2` (job `a25bb078`,
AMD64 dedicated) both ended `Failed` with `ResponseCode 127` and stderr
`bash: /opt/dda/DefectDetectionApplication/scripts/portal-build-agent.sh:
No such file or directory`. Live inspection of runner `i-0b8221f5ed2ebc2a9`
confirmed `/opt/dda/DefectDetectionApplication` exists (the clone worked)
while the script does not. Because the agent is what performs the
`SOURCE_REF` checkout, the mechanism that was supposed to fetch the source
is itself only obtainable via that checkout.

The plumbing for the feature is already half-present, which shapes the whole
design: **extend it, do not reinvent it.**

- `build_jobs.py:103`, `build_domain.py:811`, and `build_fleet.py:112` all
  carry `'source_ref': None,  # None -> the repo default branch` in the
  build config.
- `build_dispatcher.py:239-241` already appends `SOURCE_REF=<ref>` to the
  agent command when `config_snapshot.source_ref` is set.
- `scripts/portal-build-agent.sh:155-176` already implements the sync:
  `git fetch --prune origin`, then `git checkout --force -B <ref>
  origin/<ref>` for a branch, else `git checkout --force <ref>`, with
  distinct failure emissions per case. Its `REPO_DIR` is derived from the
  script's own location (line 123), so the agent does not care *where* the
  tree lives — only the dispatcher's invocation path does.
- Build config lives in the settings table under
  `build_infrastructure_config`, served by `build_config.py` on
  `/build-config`; `KNOWN_PARAMETERS = tuple(build_domain.DEFAULT_BUILD_CONFIG)`,
  so a new default field becomes an operator-settable parameter for free.

So `source_ref` exists as a global config value with no UI and no
per-submission override, and repository selection does not exist at all.

The work splits into two deployable increments:

**Increment A (unblocking).** Three changes make a build with an explicit
ref run end to end:

1. **One authoritative repository directory.** `build_dispatcher.py:112-113`
   defaults `BUILD_REPO_DIR=/opt/dda/DefectDetectionApplication` (the
   deployed Lambda sets no override — verified), while `build_fleet.py:179`
   clones dedicated servers to `/home/ubuntu/DefectDetectionApplication`.
   The dedicated agent path is never where the repo is. Because existing
   dedicated servers must keep working without re-bootstrap (Req 5.3),
   `/home/ubuntu/DefectDetectionApplication` becomes the single default,
   the bootstrap records the directory it actually used, and the dispatcher
   prefers that recorded value.
2. **A bootstrap completion gate for ephemeral runners.** The agent command
   for job `1ce014a3` was requested at 21:36:59Z; cloud-init finished at
   21:38:54Z (`Up 140.42 seconds`). `runner_bootstrap_user_data()`
   (`build_dispatcher.py:254-278`) clones and runs `setup-build-server.sh`
   with no completion signal, while the dedicated path already writes
   `/var/log/dda-build-server-bootstrap.done`. The ephemeral bootstrap
   gains the same marker and the dispatcher gates the agent SendCommand on
   observing it, with a bounded budget.
3. **Ref-aware bootstrap.** The bootstrap syncs the selected repository and
   ref *before* the agent is invoked, using command text produced by one
   shared generator so the agent's existing `SOURCE_REF` contract stays the
   only sync semantics in the system.

After Increment A a build submitted with an explicit ref runs end to end.

**Increment B (the feature).** An operator-controlled default repository in
build config, per-submission repository + ref in the API and
`config_snapshot`, a branch-discovery endpoint, the frontend repository
field and branch dropdown, and recording of the resolved commit SHA.

## Glossary

- **Bug_Condition (C)**: an input triggers the blocking defect when a
  Build_Job is dispatched and ANY of: (a) the tree the agent is invoked from
  is not guaranteed to contain `scripts/portal-build-agent.sh` for the job's
  selected ref, (b) the agent path prefix differs from the directory that
  server's bootstrap cloned into, or (c) the agent command is sent before
  the runner's bootstrap has signalled completion.
- **Feature_Gap (G)**: a submission cannot express a repository at all, and
  can express a ref only by changing global configuration.
- **Property (P)**: the runner obtains exactly the selected `(repository,
  ref)` before the agent runs; the agent path equals the bootstrap clone
  directory; no agent command precedes an observed readiness signal; the
  selected source is visible on the job record.
- **Preservation**: the zero-effort submission path, builds authorization,
  the `server-index` None-key omission, all existing build API shapes, the
  `config_snapshot` key set, and the agent's argument/emission contract are
  unchanged.
- **Source_Selection**: the pair `(repository, source_ref)` chosen at
  submission time, resolved against build config defaults and persisted in
  `config_snapshot`.
- **Repo_Dir**: the on-server directory holding the repository clone.
  Authoritative default `/home/ubuntu/DefectDetectionApplication`
  (matching `build_fleet.py`'s existing dedicated bootstrap, so existing
  servers need no re-bootstrap).
- **Bootstrap_Marker**: `/var/log/dda-build-server-bootstrap.done`, written
  as the last step of bootstrap. Already produced by the dedicated
  `USER_DATA_TEMPLATE` (`build_fleet.py`); added to the ephemeral path.
- **Bootstrap_Log**: `/var/log/dda-build-server-bootstrap.log`, the location
  recorded on the job for diagnosis (Req 6.4).
- **Source_Sync**: `git fetch --prune origin` followed by
  `git checkout --force -B <ref> origin/<ref>` for a branch or
  `git checkout --force <ref>` otherwise — the semantics
  `portal-build-agent.sh:155-176` already implements.
- **Sync_Generator**: the single pure function producing Source_Sync command
  text, used by the ephemeral user-data, the dedicated user-data, and the
  pre-agent preamble, so the three cannot drift.
- **Discovery**: `GET /build-branches?repository=<url>` returning the
  repository's branches and its default branch.
- **Normalized_Repository**: an `https://<allowed-host>/<owner>/<repo>`
  URL produced by the validator; discovery URLs are built only from the
  parsed `<owner>/<repo>`, never from raw user input (Req 3.5).

## Bug Details

### Bug Condition

The blocking defect has three facets, all provable statically and all
confirmed by the 2026-08-06 evidence. The feature gap is separate and does
not fail builds; it only prevents expressing the source.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input containing
           job         : BuildJob (execution_mode, config_snapshot)
           server      : BuildServer or NULL       -- dedicated only
           bootstrap   : { marker_observed, finished_at } or NULL
           command     : the agent invocation the dispatcher would send
           now         : timestamp
  OUTPUT: boolean

  -- (a) The agent script is not obtainable from the tree it is run from.
  agentUnobtainable :=
      command IS PRESENT
      AND NOT syncedBeforeAgentInvocation(command,
                                          selectedRepository(job),
                                          selectedRef(job))
      AND NOT agentScriptExistsOn(defaultBranchOf(selectedRepository(job)))

  -- (b) Path contract mismatch between bootstrap and dispatcher.
  pathMismatch :=
      agentPathPrefix(command) != bootstrapRepoDir(job, server)

  -- (c) Bootstrap race: command sent before the runner is ready.
  bootstrapRace :=
      command IS PRESENT
      AND job.execution_mode = 'ephemeral'
      AND (bootstrap IS NULL OR NOT bootstrap.marker_observed)

  RETURN agentUnobtainable OR pathMismatch OR bootstrapRace
END FUNCTION


FUNCTION isFeatureGap(submission)
  RETURN submission CANNOT express a repository
      OR (submission CANNOT express a ref
          AND ref comes only from global build configuration)
END FUNCTION
```

### Correct Result Predicate

```
FUNCTION expectedBehavior(dispatch)
  RETURN sourceSyncedBeforeAgent(dispatch)                      -- Req 4.1
     AND syncCommandsComeFromOneGenerator(dispatch)             -- Req 4.3
     AND agentPathPrefix(dispatch) = bootstrapRepoDir(dispatch) -- Req 5.1/5.4
     AND agentSentOnlyAfterMarkerObserved(dispatch)             -- Req 6.1/6.2
     AND (NOT bootstrapExceededBudget(dispatch)
          OR jobFailedWithStage(dispatch, 'bootstrap'))         -- Req 6.3
     AND (NOT sourceUnobtainable(dispatch)
          OR jobFailedNaming(dispatch, repository, ref)
             AND failureClass(dispatch)
                 IN {'ref_not_found', 'repository_unreachable'}) -- Req 4.4
     AND snapshotCarries(dispatch, repository, source_ref)      -- Req 1.6/2.5
END FUNCTION
```

### Concrete Examples

- **Ephemeral 127 (observed)**: job `1ce014a3`, SSM `e9281bdc`, JP5
  ephemeral. Bootstrap cloned `origin/main` into
  `/opt/dda/DefectDetectionApplication`; the dispatcher invoked
  `/opt/dda/DefectDetectionApplication/scripts/portal-build-agent.sh`;
  the script is absent from `main`. Expected: the bootstrap syncs the
  selected ref (which carries the script) and the agent runs. Actual:
  exit 127, generic failure.
- **Dedicated 127 (observed)**: job `a25bb078`, SSM `d75f1ea2`, AMD64
  dedicated. Two independent causes stack: the bootstrap put the clone at
  `/home/ubuntu/DefectDetectionApplication` while the dispatcher targeted
  `/opt/dda/...`, and `main` lacks the script anyway. Expected: agent path
  equals the bootstrap directory and the ref is synced first.
- **Bootstrap race (observed)**: agent command requested 21:36:59Z,
  cloud-init finished 21:38:54Z (`Up 140.42 seconds`). Expected: no agent
  command until `/var/log/dda-build-server-bootstrap.done` is observed.
- **Partial bootstrap (observed)**: the same runner logged
  `Failed: sudo chmod 666 /var/run/docker.sock` and
  `Failed to set Python 3.11 as default` while cloud-init still reported
  success. Expected: the marker is authoritative — the runner is treated as
  ready — and `/var/log/dda-build-server-bootstrap.log` is recorded on the
  job for diagnosis (Req 6.4).
- **Legacy dedicated server**: a server bootstrapped weeks ago has the repo
  at `/home/ubuntu/DefectDetectionApplication` and no recorded directory
  field. Expected: resolution falls back to the authoritative default and
  the build runs with no re-bootstrap (Req 5.3).
- **Ref not found**: a user submits `feature/typo`. Expected: the job fails
  naming the repository and the ref, classified `ref_not_found` — not a
  bare 127.
- **Repository unreachable**: a user submits a private fork URL. Expected:
  classified `repository_unreachable`, naming the repository.
- **Non-branch ref**: a user pastes a 40-hex SHA. Expected: accepted, and
  the sync takes the detached `git checkout --force <ref>` path the agent
  already implements (Req 2.7).
- **Discovery on a private repository**: expected a distinct
  "not accessible" error, not an empty branch list presented as success
  (Req 3.3).
- **Zero-effort submission**: a user submits JP5 ephemeral without touching
  the new fields. Expected: byte-identical request shape to today, `201`,
  one queued job, default repository, default branch (Req 7.1).

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Submitting with no repository/ref selection produces the same request
  shape, the same `201`, and the same single queued Build_Job per target
  (Req 7.1).
- The builds role-permission matrix and 403 envelope, including the
  `build-fleet-rbac-visibility` fixes (Req 7.2).
- `without_null_index_keys` / `INDEXED_KEY_ATTRIBUTES`: None-valued indexed
  key attributes stay omitted, so ephemeral submission keeps working
  against the `server-index` GSI (Req 7.3).
- `GET /builds`, `GET /builds/{id}`, `GET /builds/{id}/logs` shapes,
  pagination tokens, ordering, cancel semantics, and error envelopes
  (Req 7.4).
- `config_snapshot` is extended with additive keys only; every existing key
  keeps its name, value, and type, and `effective_build_config`'s
  None-means-default semantics are unchanged (Req 7.5).
- `portal-build-agent.sh`'s argument contract (`BUILD_JOB_ID`,
  `BUILD_TARGET`, `EVENT_BUS`, `SOURCE_REF`), exit codes (0/64/75), lock
  behavior, and phase-event emissions (Req 7.6).
- The `portal_builds` suite and the builds-related backend suites pass
  unchanged (Req 7.7). `portal_builds` runs with `--noconftest`.

**Scope:**
Inputs outside `isBugCondition` and `isFeatureGap` are unaffected:
successful phase-event handling, cancellation, retry, queue promotion and
serialization, dedicated allocation locking, target-to-architecture
mapping, fleet lifecycle actions, and every non-builds portal surface.

## Hypothesized Root Cause

All four are **confirmed** by live evidence plus static inspection; none is
speculative.

1. **Bootstrap ordering (confirmed)**: both bootstrap paths run
   `git clone <url>` with no ref, so the tree is the remote default branch.
   The agent script that performs the `SOURCE_REF` checkout lives only on a
   non-default branch. The sync can therefore never execute. Evidence:
   commit `479ab7f` reachable only from
   `origin/feature/portal-build-fleet-and-workflow-gates`; runner
   `i-0b8221f5ed2ebc2a9` has the clone but not the script; two SSM
   invocations with `ResponseCode 127`.
2. **Repo-directory mismatch (confirmed)**: `build_dispatcher.py:112-113`
   default `/opt/dda/DefectDetectionApplication` versus
   `build_fleet.py:179` clone into `/home/ubuntu/DefectDetectionApplication`,
   with no `BUILD_REPO_DIR` override on the deployed Lambda. Two literals,
   no shared source of truth.
3. **No ephemeral readiness signal (confirmed)**:
   `runner_bootstrap_user_data()` (`build_dispatcher.py:254-278`) writes no
   marker and the dispatcher's only gate is
   `instance_ssm_online(instance_id)` — the SSM agent pings Online long
   before `setup-build-server.sh` finishes. The dedicated path already has
   the marker (`build_fleet.py` `USER_DATA_TEMPLATE`), so the pattern to
   mirror already exists.
4. **No per-submission source (confirmed by inspection)**:
   `validate_build_request` ignores any repository/ref in the body,
   `create_build_jobs` snapshots only the global config, and
   `SubmitBuildRequest` / `BuildsPage.tsx` have no such fields.

## Correctness Properties

Property 1: Bug Condition - Agent path equals the bootstrap clone directory

_For any_ Build_Job in either execution mode and any configured repository
directory (default or overridden), the agent command's path prefix SHALL
equal the directory that job's bootstrap cloned into, and both SHALL be
obtained from the one shared resolver rather than from independent literals.

**Validates: Requirements 5.1, 5.2, 5.4**

Property 2: Preservation - Legacy dedicated servers need no re-bootstrap

_For any_ Build_Server record that carries no recorded repository directory
(every server bootstrapped before this change), resolution SHALL yield
`/home/ubuntu/DefectDetectionApplication` — the location those servers'
bootstrap actually used — and the dedicated dispatch path SHALL otherwise be
identical to today's.

**Validates: Requirements 5.3**

Property 3: Bug Condition - The runner obtains the selected source before the agent runs

_For any_ selected `(repository, ref)`, the bootstrap command sequence SHALL
perform the Source_Sync for that pair strictly before any agent invocation,
so that a ref whose content includes `scripts/portal-build-agent.sh` but
whose repository default branch does not SHALL still execute the agent. No
dispatch path SHALL depend on the agent script being present on the default
branch.

**Validates: Requirements 4.1, 4.2**

Property 4: Preservation - One sync mechanism, idempotent under repetition

_For any_ `(repository, ref)`, the pre-agent Source_Sync command text SHALL
be produced by the same Sync_Generator used by both user-data paths and
SHALL express exactly the semantics `portal-build-agent.sh:155-176` already
implements; applying the pre-agent sync and then the agent's own sync SHALL
leave the same `HEAD` as the agent's sync alone; and the agent's argument
contract, exit codes, and phase emissions SHALL be unchanged.

**Validates: Requirements 4.3, 7.6**

Property 5: Bug Condition - Source failures are named and classified

_For any_ unobtainable source, the Build_Job SHALL fail with a message
naming both the repository and the ref and a class distinguishing
`ref_not_found` from `repository_unreachable`, and SHALL NOT surface a bare
exit 127 or a generic agent-command failure.

**Validates: Requirements 4.4**

Property 6: Bug Condition - Bootstrap completion gates the agent command

_For any_ sequence of dispatcher ticks over a provisioning runner, no agent
command SHALL be sent in any tick where the Bootstrap_Marker has not been
observed; the readiness decision SHALL be a function of the marker probe
alone (never elapsed-sleep); a marker observed after inner-step failures
SHALL still count as ready with the Bootstrap_Log location recorded; and
elapsed time strictly beyond the configured budget SHALL fail the job with
`bootstrap` recorded as the failing stage rather than waiting indefinitely.

**Validates: Requirements 6.1, 6.2, 6.3, 6.4**

Property 7: Bug Condition - Provisioning/bootstrap failures release the runner

_For any_ Build_Job that fails during provisioning or bootstrap, the failure
path SHALL request termination of that job's compute in the same pass that
records the failure. (The reconciliation half — sweeping runners orphaned by
lost notifications — is owned by `build-fleet-execution-failures` and is not
implemented here.)

**Validates: Requirements 6.5**

Property 8: Repository validation is total and containment-safe

_For any_ string supplied as a repository, validation SHALL either reject it
with the standard validation envelope naming the `repository` field and
create no Build_Job, or accept it and yield a Normalized_Repository that is
HTTPS, host-allowlisted, and of the form `<owner>/<repo>` with no userinfo,
port, query, or fragment; _for any_ input, every outbound discovery URL
SHALL be constructed solely from the parsed `<owner>/<repo>` against the
fixed API host, so no input can direct a request at a non-repository
endpoint.

**Validates: Requirements 1.3, 1.4, 3.5**

Property 9: Source_Selection snapshot fidelity

_For any_ accepted submission, each created Build_Job's `config_snapshot`
SHALL carry the resolved repository and `source_ref` exactly as resolved:
the submitted values when supplied, the configured default repository when
the repository is omitted, and the configured `source_ref` (`None` meaning
the repository default branch) when the ref is omitted; tags and 40-hex
SHAs SHALL be accepted unchanged; and the configured default SHALL come
from the `build_infrastructure_config` setting, not a frontend constant.

**Validates: Requirements 1.2, 1.5, 1.6, 2.4, 2.5, 2.7**

Property 10: Discovery result classification

_For any_ upstream outcome — success, empty repository, 404, rate-limited
403, non-rate-limit 403, timeout, 5xx, malformed payload — Discovery SHALL
return the branch list with exactly one branch flagged default on success,
and otherwise a distinct actionable error code per condition; no failure
SHALL be reported as a success with an empty list; and success against a
public repository SHALL require no credentials.

**Validates: Requirements 3.1, 3.2, 3.3**

Property 11: Discovery authorization

_For any_ role, Discovery SHALL succeed if and only if the role holds the
builds read permission, otherwise returning the existing 403 envelope and
recording one denial in the existing audit structure.

**Validates: Requirements 3.4**

Property 12: Frontend source selection behavior

_For any_ configured default repository the submission form SHALL pre-fill
the repository field with it; _for any_ repository value the branch dropdown
SHALL be populated from Discovery for that repository and SHALL re-populate
when the repository changes; _for any_ Discovery outcome exactly one of
loading / actionable error / options SHALL be presented, with manual ref
entry and submission still available on failure; and the build detail view
SHALL display the repository and ref the job was built from.

**Validates: Requirements 1.1, 2.1, 2.2, 2.3, 2.6**

Property 13: Resolved commit recording

_For any_ build whose source sync resolves a commit, the Build_Job SHALL
record that commit SHA; _for any_ event payload lacking it (legacy agents),
the record SHALL remain valid and unchanged in every other field.

**Validates: Requirements 4.5**

Property 14: Preservation - Everything outside the selected source is unchanged

_For any_ submission that selects neither repository nor ref, the fixed
system SHALL produce the same request shape, `201`, and single queued
Build_Job per target as the original; _for any_ role and builds operation
the authorization outcome, 403 envelope, and audit structure SHALL be
unchanged; _for any_ generated job the stored item SHALL still omit
None-valued indexed key attributes; _for any_ list/detail/logs/cancel call
the response shape, pagination token, ordering, and error envelope SHALL be
unchanged; and `config_snapshot` SHALL differ from the original only by
additive keys. Concretely: the `portal_builds` suite and the builds-related
backend suites pass unchanged.

**Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5, 7.7**

## Fix Implementation

### Boundary with build-fleet-execution-failures

`.kiro/specs/build-fleet-execution-failures/` (all 13 tasks unstarted) owns
SSM/agent **outcome reconciliation** (`GetCommandInvocation`, invocation
evidence, settlement windows, execution diagnostics), **runtime accounting**
(queue/provisioning/execution split, heartbeat and progress leases, hard
ceilings), and the `provisioning` **stuck-job / orphaned-runner
reconciliation**. This spec owns getting the *right source* onto the runner
and starting the agent only when the runner is ready.

| Concern | Owner |
|---|---|
| Repository directory alignment, one authoritative value | **this spec** (its design lists the mismatch as hypothesis 1 but explicitly does not fix it) |
| Ref-aware bootstrap, Sync_Generator, source-failure classes | **this spec** |
| Bootstrap completion gate and bootstrap-stage failure | **this spec** |
| Per-submission repository/ref, discovery, UI | **this spec** |
| Retrieving and persisting SSM invocation evidence, diagnostics redaction/bounds | build-fleet-execution-failures |
| Timeout classification, runtime budgets, heartbeat/progress leases | build-fleet-execution-failures |
| Orphan sweep / stuck-`provisioning` reconciliation, exactly-once terminal effects | build-fleet-execution-failures |
| Build Log diagnostic payload and its UI rendering | build-fleet-execution-failures |

**Overlap risk — do not edit these in parallel without coordinating:**

- `edge-cv-portal/backend/functions/build_dispatcher.py` — both specs change
  it. This spec touches `BUILD_REPO_DIR`, `agent_command`,
  `runner_bootstrap_user_data`, `provision_ephemeral`,
  `verify_and_start_dedicated`. The other spec adds command reconciliation
  and preflight in the same module. Land this spec's Increment A first (it
  is the unblocking change), then rebase the other spec's work onto it.
- `edge-cv-portal/backend/functions/build_events.py` — this spec adds only
  an additive read of a commit SHA from the existing `phase=building`
  payload in `apply_phase_event` (Increment B, Req 4.5). The other spec
  restructures the same function's classification and terminal-effects
  handling. Keep this spec's change to a single additive field so a
  three-way merge stays trivial, or defer it until the other spec's
  reconciliation lands.
- `edge-cv-portal/backend/functions/build_jobs.py` — lesser overlap: this
  spec extends `submit_build` and adds the discovery route; the other spec
  extends the logs endpoint with a diagnostic payload. Different functions,
  same module.

Also out of scope here: the missing **cancel control** in the builds UI
(`api.cancelBuild()` and `POST /builds/{id}/cancel` both exist, no component
references them) — tracked separately; and builds **authorization**, owned by
`build-fleet-rbac-visibility`.

### Increment A — unblocking

#### A1. One authoritative repository directory (Req 5)

**New file**: `edge-cv-portal/backend/functions/build_source.py` — a pure
module (no AWS clients) that both the dispatcher and the fleet handler
import, mirroring how `build_domain.py` / `build_planner.py` already hold
pure decisions.

```python
#: The single authoritative on-server clone location. Matches the location
#: build_fleet.py's existing dedicated bootstrap already uses, so servers
#: bootstrapped before this change keep working untouched (Req 5.3).
DEFAULT_REPO_DIR = '/home/ubuntu/DefectDetectionApplication'

def resolve_repo_dir(job, server=None, env_default=None) -> str:
    """Repo_Dir for a Build_Job: the directory the bootstrap recorded for
    this server/runner, else the configured env default, else
    DEFAULT_REPO_DIR (Req 5.1, 5.2, 5.3, 5.4)."""

def agent_script_path(repo_dir: str) -> str:
    """f'{repo_dir}/scripts/portal-build-agent.sh'"""
```

**File**: `build_dispatcher.py`

- `BUILD_REPO_DIR` default changes from `/opt/dda/DefectDetectionApplication`
  to `build_source.DEFAULT_REPO_DIR`; the env override is retained.
- `agent_command(job, repo_dir)` takes the resolved directory instead of
  reading the module constant, and builds its path via
  `build_source.agent_script_path`.
- `verify_and_start_dedicated` / `provision_ephemeral` resolve the directory
  from the server record (`server['repo_dir']`) or the runner record
  (`job['runner']['repo_dir']`) and pass it down.
- `runner_bootstrap_user_data(job)` clones into the same resolved directory
  and records it (A2 writes `runner.repo_dir` at provisioning time).

**File**: `build_fleet.py`

- `USER_DATA_TEMPLATE` stops hard-coding `/home/ubuntu/DefectDetectionApplication`
  and formats `{repo_dir}` from `build_source.DEFAULT_REPO_DIR`.
- The launch path records `repo_dir` on the Build_Server record, so the
  dispatcher reads back the exact directory the bootstrap used.

A structural test asserts no independent `DefectDetectionApplication`
directory literal remains in either module (Property 1, Req 5.2).

#### A2. Bootstrap completion gate (Req 6.1-6.4)

**File**: `build_dispatcher.py` — `runner_bootstrap_user_data()`

The ephemeral user-data mirrors the dedicated template: redirect all output
to `/var/log/dda-build-server-bootstrap.log`, and `touch
/var/log/dda-build-server-bootstrap.done` as the last statement. As in the
dedicated template, inner-step failures do not suppress the marker — the
marker means "bootstrap ran to completion", and Req 6.4 makes that signal
authoritative.

**New probe** (added to the existing synchronous `run_shell_sync` pattern):

```python
BOOTSTRAP_PROBE_COMMANDS = [
    'test -f /var/log/dda-build-server-bootstrap.done '
    '&& echo "BOOTSTRAP_DONE=1" || echo "BOOTSTRAP_DONE=0"',
    'echo "BOOTSTRAP_LOG=/var/log/dda-build-server-bootstrap.log"',
]
```

**New pure decision** in `build_planner.py` (where the other tick decisions
already live):

```python
READINESS_READY, READINESS_WAIT, READINESS_TIMEOUT = 'ready', 'wait', 'timeout'

def decide_runner_readiness(job, probe_output, now) -> ReadinessDecision:
    """READY when the marker is observed (regardless of inner-step
    failures, Req 6.4); WAIT at or below the bootstrap budget; TIMEOUT
    strictly past it (Req 6.3). Never sleep-based (Req 6.2)."""
```

The budget is a snapshotted config value `bootstrap_timeout_minutes`
(default 20; the observed bootstrap took ~140 s, so 20 minutes is generous
without being unbounded), measured from `dispatched_at`. The boundary is
strict `now > deadline`, matching the existing watchdog convention.

`provision_ephemeral`'s readiness gate becomes: SSM Online **and**
`decide_runner_readiness(...) == READY`. On `READY`, the job records
`bootstrap = {'marker_at': now, 'log_path': <probe value>}` (Req 6.4)
before the agent SendCommand. On `TIMEOUT`, `fail_job(..., ERROR_BOOTSTRAP_TIMEOUT,
'... bootstrap did not complete within N minutes; bootstrap log: <path>',
'build_bootstrap_timeout', {'stage': 'bootstrap', ...})` and terminate the
runner via the existing `terminate_partial_compute` path (Req 6.3, 6.5).

For dedicated servers the same probe runs alongside the existing pgrep
verification, but the policy differs: the marker is required only while the
server is still within its bootstrap budget from launch; a server past that
window with no marker proceeds with an advisory note recorded. This keeps
Req 5.3/7.1 intact for servers bootstrapped before this change and for any
manually prepared server, while still closing the race on freshly launched
ones. **Review point** — see "Decisions worth reviewing".

#### A3. Ref-aware bootstrap (Req 4.1-4.4)

**File**: `build_source.py` — the Sync_Generator, the single origin of all
Source_Sync command text:

```python
SYNC_MARKER = 'PORTAL_SOURCE_SYNC_FAILED'
EXIT_REPO_UNREACHABLE = 65
EXIT_REF_NOT_FOUND     = 66

def source_sync_commands(repo_url, repo_dir, source_ref) -> list[str]:
    """Clone-if-absent, then the exact Source_Sync semantics
    portal-build-agent.sh:155-176 implements: `git fetch --prune origin`,
    then `git checkout --force -B <ref> origin/<ref>` when
    refs/remotes/origin/<ref> verifies, else `git checkout --force <ref>`.
    Failures echo `PORTAL_SOURCE_SYNC_FAILED kind=<class>
    repository=<url> ref=<ref>` and exit 65 (repository unreachable) or
    66 (ref not found) — never a bare 127 (Req 4.4). An empty
    source_ref yields the clone-only sequence, i.e. today's behavior."""

def bootstrap_commands(repo_url, repo_dir, source_ref) -> list[str]:
    """source_sync_commands + `bash ./setup-build-server.sh` + the
    Bootstrap_Marker write — the body of both user-data scripts."""
```

Callers:
- `build_dispatcher.runner_bootstrap_user_data(job)` — ephemeral user-data.
- `build_fleet.USER_DATA_TEMPLATE` — dedicated launch (so a dedicated
  server is bootstrapped at the ref too).
- `build_dispatcher.agent_command(job, repo_dir)` — a **pre-agent
  preamble**: the same `source_sync_commands` run immediately before the
  agent invocation, so a dedicated server bootstrapped weeks ago on another
  ref still gets the selected source, and so an ephemeral runner whose
  user-data predates a config change is corrected. The agent's own Step 2
  then re-runs the identical sync, which is idempotent (Property 4).

Failure surfacing (Req 4.4): the preamble, on a sync failure, emits one
`dda.portal.builds` / `BuildPhaseChange` event with `phase=failed`,
`error_kind=source_sync`, `source_error=<ref_not_found|repository_unreachable>`,
and a message naming repository and ref, then exits with its dedicated
code. This rides the **existing** phase-event pipeline
(`build_events.handle_phase_event`) rather than adding SSM invocation
reads, keeping the boundary with `build-fleet-execution-failures` clean.
The inline `aws events put-events` call in the preamble is a deliberate,
tested minimal duplication of the agent's `emit_event` helper — the agent
helper cannot be reused because it lives in the tree the preamble is
fetching. A property test asserts both paths produce the same detail shape.

`build_events.py` needs no change for this: `apply_phase_event` already
handles `phase=failed` with an `error_kind`, and `error_kind=source_sync`
is not `publishing`, so it takes the build-stage failure edge.

### Increment B — the feature

#### B1. Operator-controlled default repository (Req 1.5)

**File**: `build_domain.py`

- `DEFAULT_BUILD_CONFIG` gains
  `'default_repository': 'https://github.com/awslabs/DefectDetectionApplication'`.
  Because `build_config.KNOWN_PARAMETERS = tuple(build_domain.DEFAULT_BUILD_CONFIG)`,
  it becomes an operator-settable, audited parameter with no change to
  `build_config.py`.
- `validate_build_config` gains rule `RULE_CONFIG_REPOSITORY_INVALID`,
  delegating to `build_source.normalize_repository_url`.

**File**: `build_jobs.py` — `DEFAULT_BUILD_CONFIG` is a duplicate literal of
the domain constant. Replace it with a reference to
`build_domain.DEFAULT_BUILD_CONFIG` (the module already imports
`build_domain`) so the parameter table has one definition. `build_fleet.py`
carries a third copy used only for read defaults; point it at the same
constant. **Review point.**

#### B2. Repository/ref validation and normalization (Req 1.3, 1.4, 2.7, 3.5)

**File**: `build_source.py`

```python
ALLOWED_REPOSITORY_HOSTS = ('github.com',)

def normalize_repository_url(value) -> tuple[Optional[str], Optional[dict]]:
    """(normalized_url, None) or (None, error) with `rule` and `field`.
    Accepts only https, an allowlisted host, and a `<owner>/<repo>` path
    (optional `.git`); rejects userinfo, ports, query, fragment, extra
    path segments, and non-string input."""

def parse_owner_repo(normalized_url) -> tuple[str, str]

def normalize_source_ref(value) -> tuple[Optional[str], Optional[dict]]:
    """Accepts branch names, tags, and 40-hex SHAs; rejects control
    characters, whitespace, leading '-', '..', and git-invalid forms
    (Req 2.7 keeps non-branch refs valid)."""
```

**File**: `build_domain.py` — `validate_build_request` gains
`RULE_REPOSITORY_INVALID` and `RULE_SOURCE_REF_INVALID`, each naming the
offending field so the existing `BUILD_REQUEST_INVALID` envelope carries it
(Req 1.4). No Build_Job is created on rejection — that already follows from
the existing accept-then-persist ordering (`test_persistence_iff_accept.py`).

#### B3. Snapshot the selection (Req 1.6, 2.4, 2.5)

**File**: `build_jobs.py` — `submit_build`

After validation, resolve the pair and extend the snapshot in place
(additive keys only, Req 7.5):

```python
config['repository'] = normalized_repository or config['default_repository']
if submitted_ref is not None:
    config['source_ref'] = submitted_ref      # else keep the config value
```

`create_build_jobs` is unchanged — it deep-copies whatever
`config_snapshot` it is handed, so each job gets its own copy (Req 7.5).
The `build_requested` audit details gain `repository` and `source_ref`.

#### B4. Branch discovery (Req 3)

**File**: `build_source.py` — pure classification with an injected fetch,
mirroring `vllm_fit_check._default_hf_fetch`:

```python
GITHUB_API_HOST = 'https://api.github.com'
DISCOVERY_TIMEOUT_SECONDS = 5
MAX_BRANCH_PAGES = 3          # 100 per page -> 300 branches, then truncated

def discover_branches(normalized_url, fetch=_default_fetch) -> DiscoveryResult:
    """Branches + default branch for a Normalized_Repository. The URLs are
    built only from parse_owner_repo() against GITHUB_API_HOST (Req 3.5)
    and carry no credentials (Req 3.2). Every failure is a distinct code
    (Req 3.3):
      REPOSITORY_NOT_FOUND     404
      REPOSITORY_FORBIDDEN     403 without rate-limit indication
      DISCOVERY_RATE_LIMITED   403/429 with rate-limit indication
      DISCOVERY_TIMEOUT        socket timeout
      DISCOVERY_UPSTREAM_ERROR 5xx / malformed payload
      REPOSITORY_EMPTY         reachable but no branches
    """
```

**File**: `build_jobs.py` — a new `@require_builds_read()` handler
`list_build_branches(event, context)` for `GET /build-branches?repository=`,
routed from `handler` alongside the existing paths. Reusing `build_jobs.py`
keeps the builds read decorator, the error envelope helper, and the audit
wiring identical to the rest of the builds surface (Req 3.4).

**File**: `infrastructure/lib/build-fleet-stack.ts` — register
`api.root.addResource('build-branches')` with `GET` on `jobsIntegration`,
next to the existing `/builds`, `/build-servers`, `/build-config`
registrations. The route-salt hash already rolls a new deployment when the
route table changes.

#### B5. Frontend (Req 1.1-1.4, 2.1-2.4, 2.6)

**File**: `frontend/src/pages/builds/types.ts`

- `SubmitBuildRequest` gains optional `repository?: string` and
  `source_ref?: string`.
- New `BuildBranchesResponse { branches: string[]; default_branch: string; truncated: boolean }`.
- `BuildJob['config_snapshot']` typing gains the optional `repository`,
  `source_ref`, and `source_commit` fields.

**File**: `frontend/src/services/api.ts` — `listBuildBranches(repository)`
alongside `submitBuild`, plus reuse of the existing build-config read for
the default repository.

**File**: `frontend/src/pages/builds/BuildsPage.tsx`

- A `FormField` + `Input` for the repository, pre-filled from
  `default_repository` on the effective build config (Req 1.1, 1.5).
- An **`Autosuggest`** (not `Select`) for the branch: it gives the dropdown
  of discovered branches *and* accepts a typed value, which is what
  Req 2.3 (manual entry when discovery fails) and Req 2.7 (tags/SHAs) both
  require. `statusType` carries loading / error, with the error text taken
  from the discovery error code. Discovery re-runs, debounced, whenever the
  repository value settles (Req 2.2).
- Submission includes `repository`/`source_ref` only when non-default and
  non-empty, so the zero-effort request body is byte-identical to today's
  (Req 1.2, 7.1).

**File**: `frontend/src/pages/builds/BuildDetail.tsx` — add repository, ref,
and resolved commit rows to the existing key-value pairs, with a placeholder
for legacy jobs that lack them (Req 2.6).

#### B6. Resolved commit (Req 4.5)

**File**: `scripts/portal-build-agent.sh` — the `phase=building` event
detail gains `"source_commit":"<git rev-parse HEAD>"`. Additive only: the
argument contract, exit codes, and existing fields are untouched (Req 7.6).

**File**: `build_events.py` — `apply_phase_event` copies a present
`source_commit` into `updates['source_commit']` and ignores its absence.
This is the single additive line flagged in the boundary table above.

## Testing Strategy

### Validation Approach

Observation-first per defect, per increment. For each of the three blocking
defects: write a test that FAILS on unfixed code and documents the
counterexample, capture the preservation baseline from unfixed code, then
fix, then re-run both. The feature work (Increment B) is new behavior, so it
follows plain property + example testing plus the preservation suites.

Environments: backend `pytest` from `edge-cv-portal/backend`; the
`portal_builds` suite under `test/backend-test/portal_builds` **requires
`--noconftest`**. Frontend `npm test` (vitest + fast-check) in
`edge-cv-portal/frontend`. Shell-level checks for the generated command text
run against a local temporary git fixture — no AWS, no EC2, no SSM.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE
implementing the fix. Confirm or refute the root cause analysis. If we
refute, we will need to re-hypothesize.

**Test Plan**:

1. **Path mismatch (root cause 2)** — `test/backend-test/portal_builds/
   test_source_selection_exploration.py`: build a dedicated Build_Job and
   the fleet-bootstrapped server record, then assert
   `build_dispatcher.agent_command(job)`'s path prefix equals the directory
   `build_fleet.USER_DATA_TEMPLATE` clones into. On unfixed code this FAILS
   (`/opt/dda/...` vs `/home/ubuntu/...`).
2. **Agent unobtainable (root cause 1)** — same file, a local git fixture:
   create a temporary origin whose default branch omits
   `scripts/portal-build-agent.sh` while a feature branch contains it, run
   the bootstrap command text the dispatcher generates, then attempt the
   agent invocation. On unfixed code the invocation fails with the missing
   file (reproducing the observed 127 class); the sync never runs.
3. **Bootstrap race (root cause 3)** — same file: drive `provision_ephemeral`
   over a provisioning job whose runner reports SSM Online while no
   Bootstrap_Marker exists, and assert no agent SendCommand is issued. On
   unfixed code this FAILS: the command is sent on the first Online tick,
   exactly as at 21:36:59Z against a bootstrap that finished at 21:38:54Z.

**Expected Counterexamples**:
- `agent_command` prefix `/opt/dda/DefectDetectionApplication` versus
  bootstrap directory `/home/ubuntu/DefectDetectionApplication` (root
  cause 2).
- Generated bootstrap performs `git clone` with no ref; the resulting tree
  lacks `scripts/portal-build-agent.sh`; agent invocation fails with
  "No such file or directory" (root cause 1).
- SendCommand issued while the marker is absent, with no bounded budget and
  no bootstrap-stage failure available (root cause 3).

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the
fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL dispatch WHERE isBugCondition(dispatch) DO
  fixed := resolveAndDispatch(dispatch)
  ASSERT agentPathPrefix(fixed) = bootstrapRepoDir(fixed)         -- P1
  ASSERT sourceSyncedBeforeAgent(fixed)                           -- P3
  ASSERT syncCommandsFromOneGenerator(fixed)                      -- P4
  ASSERT NOT agentSentBeforeMarkerObserved(fixed)                 -- P6
  IF bootstrapExceededBudget(fixed) THEN
    ASSERT failedWithStage(fixed, 'bootstrap')                    -- P6
    ASSERT terminationRequested(fixed)                            -- P7
  IF sourceUnobtainable(fixed) THEN
    ASSERT failureNames(fixed, repository, ref)                   -- P5
    ASSERT failureClass(fixed) IN {ref_not_found,
                                   repository_unreachable}        -- P5
END FOR
```

The exploration tests from the previous phase are re-run unchanged; each
inverts into the fix assertion for its property.

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold,
the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) AND NOT isFeatureGap(input) DO
  ASSERT original(input) = fixed(input)
  -- submit shape/201/one queued job per target       (Req 7.1)
  -- role x builds operation authorization + envelope (Req 7.2)
  -- stored item omits None indexed keys              (Req 7.3)
  -- list/detail/logs/cancel shapes, tokens, order    (Req 7.4)
  -- config_snapshot differs only by additive keys    (Req 7.5)
  -- agent argument contract, exit codes, emissions   (Req 7.6)
END FOR
```

**Testing Approach**: Property-based testing is recommended for
preservation checking because:
- It generates many test cases automatically across the input domain
- It catches edge cases that manual unit tests might miss
- It provides strong guarantees that behavior is unchanged for all
  non-buggy inputs

**Test Plan**: Record the pre-change oracle on unfixed code — the exact
`POST /builds` body for a no-selection submission, the resulting job records
and `config_snapshot` key set, the stored-item attribute set for an
ephemeral job, and the list/detail/logs/cancel response shapes — then assert
the fixed code matches it except for additive keys. Re-run the whole
`portal_builds` suite (`--noconftest`) and the builds-related backend suites
after each increment.

**Test Cases**:
1. **Zero-effort submit**: no `repository`/`source_ref` in the body →
   identical request shape, `201`, one queued job per target, snapshot
   `repository` = configured default, `source_ref` = configured value.
2. **Indexed keys**: ephemeral job with `server_id = None` → stored item
   omits `server_id`, keeps `predecessor_job_id: None`.
3. **`config_snapshot` extension**: every pre-change key present with the
   same value/type; the only new keys are `repository`, `source_ref`
   (already present), and `source_commit`.
4. **Agent contract**: argument permutations and missing-argument cases
   still exit 64/75/0 as documented; the `building`/`succeeded`/`failed`
   detail shapes keep their existing fields.
5. **Legacy servers**: server records without `repo_dir` resolve to
   `/home/ubuntu/DefectDetectionApplication`; the dedicated dispatch path
   is otherwise unchanged.
6. **Suites**: `portal_builds` (`--noconftest`) and the builds-related
   backend suites pass unchanged after each increment.

### Unit Tests

- `build_source.normalize_repository_url`: accepts the DDA URL with and
  without `.git` and a trailing slash; rejects `http://`, `git@`, userinfo,
  a port, a query, a fragment, an extra path segment, a non-allowlisted
  host, and non-string input.
- `build_source.normalize_source_ref`: accepts `main`,
  `feature/portal-build-fleet-and-workflow-gates`, `v1.2.3`, a 40-hex SHA;
  rejects whitespace, control characters, a leading `-`, and `..`.
- `build_source.resolve_repo_dir`: recorded value wins, then env override,
  then `DEFAULT_REPO_DIR`.
- `build_source.source_sync_commands`: branch case emits
  `checkout --force -B`; non-branch case emits `checkout --force`; empty ref
  emits clone-only; failure branches carry the marker plus exit 65/66.
- `build_planner.decide_runner_readiness`: marker present → READY (even
  with recorded inner-step failures); absent at the deadline → WAIT; absent
  strictly past it → TIMEOUT.
- `build_source.discover_branches`: one case per error code, plus the
  success case flagging the default branch and the truncation flag at the
  page cap.
- Frontend: repository field default; Autosuggest options from a discovery
  response; loading/error status; submit body omits the fields when
  untouched; `BuildDetail` renders repository/ref/commit and a placeholder
  when absent.

### Property-Based Tests

Backend with `hypothesis` (the repo already uses it — see `.hypothesis/`),
frontend with `fast-check` (pattern:
`frontend/src/components/vllm-publish/publishState.gating.property.test.ts`).

- **Property 1** (`hypothesis`): over generated jobs × servers × configured
  directories — `agentPathPrefix == bootstrapRepoDir`, plus the structural
  assertion that no directory literal remains in `build_dispatcher.py` or
  `build_fleet.py`.
- **Property 2**: over server records with the field absent/None/empty →
  resolution is `DEFAULT_REPO_DIR`.
- **Property 3**: over generated `(repository, ref)` pairs — the sync
  command index is strictly less than the agent-invocation index in every
  generated sequence; plus the git-fixture integration case where only the
  non-default branch carries the agent script.
- **Property 4**: over `(repository, ref)` — the preamble's command list
  equals `source_sync_commands(...)` exactly; applying the preamble then the
  agent's own sync yields the same `HEAD` as the agent's sync alone (git
  fixture); agent argument permutations behave as documented.
- **Property 5**: over the two failure classes × generated repository/ref
  values — marker line, exit code, and message content are class-distinct
  and name both values.
- **Property 6**: over generated probe/time sequences — no SendCommand
  before an observed marker; WAIT at `now == deadline`, TIMEOUT at
  `now > deadline`; marker-with-failures → READY with the log path
  recorded.
- **Property 7**: over provisioning/bootstrap failure causes — a
  termination request accompanies every recorded failure.
- **Property 8**: over arbitrary text (`hypothesis` `st.text()` plus a
  hostile corpus) — total function, and every recorded outbound URL starts
  with `https://api.github.com/repos/<owner>/<repo>` derived from the parse,
  never from raw input.
- **Property 9**: over submissions × configs — snapshot repository/ref
  equal the resolved values; omitted fields fall back to config; tags/SHAs
  survive unchanged.
- **Property 10**: over the upstream-outcome domain — distinct code per
  condition, exactly one default branch on success, no empty-list success.
- **Property 11**: over the role domain at the real decorator boundary —
  200 iff builds read, else the 403 envelope plus one denial audit record.
- **Property 12** (`fast-check`): over configured defaults, repository
  values, and discovery outcomes — pre-fill, re-population on change,
  exactly one of loading/error/options, submission still possible on
  failure.
- **Property 13**: over phase-event payloads with and without
  `source_commit` — persisted when present, record otherwise unchanged.
- **Property 14**: differential against the recorded pre-change oracle over
  generated no-selection submissions, roles, jobs, and list/detail/logs
  calls.

### Integration Tests

- **Dispatcher tick, ephemeral, end to end (mocked AWS)**: queued →
  provisioning → marker observed → agent SendCommand carrying the resolved
  repo dir, repository, and ref, in one `run_tick` sequence. Extends the
  existing `test_dispatcher_tick_integration.py` rather than replacing it.
- **Dispatcher tick, dedicated**: allocation → pgrep verification →
  readiness policy → agent command against the recorded server directory.
- **Git fixture**: a temporary origin whose default branch lacks the agent
  script; the generated bootstrap plus preamble yields a tree containing it
  and a resolvable `HEAD`; a bogus ref produces the `ref_not_found` marker
  and exit 66; an unreachable origin produces `repository_unreachable` and
  exit 65.
- **Submit → snapshot → dispatch**: `POST /builds` with a repository and a
  ref reaches the generated agent command with the same values, through the
  real handler and decorator boundary.
- **Discovery route**: `GET /build-branches` with an injected fetch —
  success, each error class, and the role matrix.
- **Frontend**: `BuildsPage` with mocked `apiService` — default pre-fill,
  branch options, repository change re-population, discovery failure with a
  manual ref still submitting; `BuildDetail` showing the recorded source.

No test in this plan launches EC2 compute, sends a real SSM command, or
starts a real build.

## Decisions worth reviewing

1. **`/home/ubuntu/DefectDetectionApplication` wins over `/opt/dda/...`.**
   Req 5.3 (existing dedicated servers keep working without re-bootstrap)
   forces this: the dedicated bootstrap already put the clone there. The
   consequence is that the *ephemeral* clone location changes, and any
   pre-baked AMI or operator runbook referencing `/opt/dda` needs updating.
   The `BUILD_REPO_DIR` env override remains, so a deployment could pin
   `/opt/dda` — but then existing dedicated servers break again, which is
   why the recorded-per-server directory exists.
2. **Dedicated readiness policy is advisory, not strict.** A strict marker
   requirement would permanently block builds on any server bootstrapped
   outside `build_fleet.py` (or one whose marker was cleaned up), which
   conflicts with Req 5.3 and 7.1. The design gates strictly on ephemeral
   runners (which Req 6.1 names) and, for dedicated servers, requires the
   marker only inside the bootstrap budget from launch. If you would rather
   fail closed on dedicated servers too, that is a one-line policy change
   in `decide_runner_readiness` — say so and I will flip it.
3. **The pre-agent preamble duplicates the agent's `emit_event` in ~4 lines
   of inline `aws events put-events`.** Unavoidable: the agent's helper
   lives in the tree the preamble is fetching. Req 4.3 is satisfied by both
   paths taking their *sync* text from one generator; the event emission is
   the small remainder. The alternative — classifying the failure in the
   Lambda from SSM invocation evidence — is `build-fleet-execution-failures`
   territory and would couple the two specs.
4. **Discovery is host-allowlisted to `github.com`.** Req 3.2 only requires
   public GitHub, and Req 3.5 requires the value cannot reach
   non-repository endpoints. An allowlist is the cheapest way to guarantee
   the latter, but it means a GitLab or CodeCommit fork is rejected at
   validation, not just undiscoverable. The allowlist is a module constant,
   trivially extendable if you want other hosts.
5. **Three copies of `DEFAULT_BUILD_CONFIG` collapse into one.**
   `build_jobs.py:103`, `build_domain.py:811`, and `build_fleet.py:112` each
   carry the table. B1 points the first and third at the second. It is a
   small refactor inside the unblocking area's blast radius, covered by a
   preservation property, but it is a refactor — flagging it rather than
   doing it silently.
6. **`bootstrap_timeout_minutes` default 20.** The observed bootstrap took
   ~140 s. Twenty minutes tolerates a cold `apt-get`/snap install on a slow
   instance while still bounding Req 6.3. It is a snapshotted config value,
   so it is operator-tunable per deployment.
