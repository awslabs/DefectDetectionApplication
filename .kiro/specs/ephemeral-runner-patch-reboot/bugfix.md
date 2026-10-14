# Bugfix Requirements Document

## Introduction

Portal ephemeral build runners are rebooted one to three minutes into the
build. The orderly shutdown stops the docker daemon, BuildKit reports
`error reading from server: EOF`, `src/edgemlsdk/build.sh` fails the build,
and the dispatcher terminates the runner. Both ephemeral builds that reached
Docker after 2026-10-01 died this way (JP5 job `b05a4f32` on 2026-10-06,
AMD64 job `8631587e` on 2026-10-07). Dedicated servers were not affected.

**Root cause (generic).** An account-level SSM State Manager patch
association runs `AWS-RunPatchBaseline` (Operation Install, default
RebootIfNeeded) on every instance as soon as it registers with SSM, not
only at its weekly cron. A fresh AMI always has pending
updates, so the patch run installs them, exits 194, and the SSM agent
reboots the host about 66 s later. The dispatcher's ephemeral readiness gate
checks only SSM Online plus the bootstrap marker, and the marker only means
the user data finished, which happened before the patch run finished.
Two secondary defects make it worse: the patch run and the bootstrap's
`apt-get` contend for the dpkg lock, and a reboot during the bootstrap
leaves the user data unfinished (cloud-init does not re-run it), so the job
would fail with `BOOTSTRAP_TIMEOUT` instead.

**Rejected fixes.** Retrying `docker build` on EOF: the host is already
shutting down. Holding snap refreshes: snap is not the cause and a hold does
not stop Patch Manager. Changing the account's patch association is an
owner-side compliance control outside this repository.

## Owner Decisions

Approved by **Ryan Vanderwerf** on **2026-10-07**.

- **P0-A — implement.** Ephemeral host-settled gate. After the
  bootstrap marker is observed, no agent command is sent while any SSM
  association on the runner is `Pending` or `InProgress` (case-insensitive)
  or while the status cannot be read. Same budget and deadline as the
  marker (`bootstrap_timeout_minutes`, default 20, from `dispatched_at`);
  strictly past it the job fails `BOOTSTRAP_TIMEOUT` naming the associations
  still running and the runner is terminated. `Success`, `Failed` and
  `Skipped` never block. Evidence on the job: `bootstrap.settled_at`,
  `bootstrap.associations`. IAM: `ssm:DescribeInstanceAssociationsStatus`
  (read-only, resource `*`) in `grantSsmCommands`.
- **P0-B — implement, ships with P0-A.** The ephemeral bootstrap survives a
  reboot: it is installed as a cloud-init per-boot script (root, 0700) whose
  first statement exits 0 when the marker exists, which then takes its own
  flock, and whose last statement is still the marker write. Every
  `apt-get` in the runner user data, `build_fleet.USER_DATA_BODY` and
  `setup-build-server.sh` carries `-o DPkg::Lock::Timeout=600`.
- **P1 — implement.** Dedicated dispatch defers, never fails, while an SSM
  association on the server is `Pending`/`InProgress`. The deferral is
  capped at 30 minutes from its start, after which the build is dispatched
  anyway with an advisory. FAIL OPEN: an unreadable status never defers
  (an advisory is recorded instead). The per-server single-build lock and
  the ambiguous-send recovery keep working.
- **P2 — deferred** (follow-ups below): Docker readiness wait in the agent
  preflight, docker snap refresh hold during builds, boot_id-based
  `RUNNER_REBOOTED` classification.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN an ephemeral runner's bootstrap marker is observed while an SSM
patch association is still running on it THEN the dispatcher sends the
agent command, and the patch reboot kills the build mid-way (BuildKit EOF).

1.2 WHEN the host reboots while the ephemeral user data is still running
THEN cloud-init never re-runs it, the marker is never written, and the job
fails `BOOTSTRAP_TIMEOUT` after the budget.

1.3 WHEN the patch run holds the dpkg lock THEN `apt-get install`/`remove`
in the bootstraps fails at once (`Could not get lock`), and the scripts
tolerate the failure.

1.4 WHEN a dedicated dispatch happens while a patch association runs on the
server THEN the build starts into a run that may reboot the server.

### Expected Behavior (Correct)

2.1 WHEN the marker is observed and any SSM association on the runner is
Pending or InProgress (any letter case) THEN the dispatcher SHALL send no
agent command that tick.

2.2 WHEN the association status cannot be read THEN the ephemeral gate SHALL
stay shut (fail-safe).

2.3 WHEN no association is Pending or InProgress THEN the dispatcher SHALL
record `bootstrap.marker_at`, `bootstrap.settled_at`,
`bootstrap.associations` and send exactly one agent command.

2.4 WHEN the gate is still shut strictly past the bootstrap deadline THEN
the job SHALL fail `BOOTSTRAP_TIMEOUT` through the existing bootstrap
failure path, with a message naming the associations still running (for
example `AWS-RunPatchBaseline (InProgress)`), and the runner SHALL be
terminated. At the deadline itself it still waits.

2.5 WHEN the host reboots before the marker is written THEN the next boot
SHALL re-run the bootstrap to completion; once the marker exists every
re-run SHALL be a no-op, and two runs SHALL never overlap.

2.6 WHEN another apt user holds the dpkg lock THEN `apt-get install` and
`remove` in the three bootstraps SHALL wait up to 600 s for it.

2.7 WHEN a dedicated dispatch finds an association Pending or InProgress
THEN it SHALL defer (job stays queued, allocation kept, advisory recorded,
nothing sent) and SHALL dispatch once none is, or with an advisory once the
deferral is strictly past 30 minutes. It SHALL never fail the job.

2.8 WHEN the dedicated status read fails THEN the dispatch SHALL proceed as
today, with an advisory recorded (fail open).

### Unchanged Behavior (Regression Prevention)

3.1 WHEN no association is running THEN ephemeral and dedicated dispatch
SHALL CONTINUE TO behave exactly as before (marker gate, pgrep
verification, dedicated bootstrap policy, preflight, attempt claim,
CloudWatch streaming).

3.2 WHEN the marker is absent THEN the marker gate SHALL CONTINUE TO decide
WAIT/TIMEOUT exactly as before; the association status is not read.

3.3 WHEN a dedicated job holds its server THEN the single-build lock, queue
promotion order and the ListCommands ambiguous-send recovery SHALL CONTINUE
TO work; an invalid preflight contract SHALL CONTINUE TO fail before any
association read.

3.4 WHEN the bootstraps run THEN the root apt line SHALL CONTINUE TO install
`git zip unzip`, the body SHALL CONTINUE TO run as `ubuntu`, a classified
sync failure SHALL CONTINUE TO exit 65/66 before the marker, and the marker
write SHALL CONTINUE TO be the last statement.

## Out of Scope and Follow-ups

- P2: Docker readiness wait in the agent preflight; docker snap refresh hold
  for the build's duration; boot_id-based `RUNNER_REBOOTED` classification.
- Changing the account's patch association (owner-side compliance control).
- `InsufficientInstanceCapacity` on m6i.4xlarge (separate; noted only).
- Any LocalServer or device code.
- Found while implementing, verified (see verification-notes.md):
  `DPkg::Lock::Timeout` does not cover the lists lock `apt-get update`
  takes (apt 2.4 and 2.8 fail it at once), so the runner's
  `apt-get update -y && apt-get install ...` line still skips its install
  if that update collides with the patch run's cache update.
- Residual risks, not verified: a reboot that interrupts `dpkg` mid-install
  would leave dpkg needing `dpkg --configure -a` on the re-run; builds
  already running on a dedicated server when the weekly patch run starts
  stay exposed (P1 only stops new dispatches); an association stuck
  `Pending` fails ephemeral jobs at the budget (the message names it).

