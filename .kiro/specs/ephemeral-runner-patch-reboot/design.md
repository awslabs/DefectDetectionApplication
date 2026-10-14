# Design — ephemeral-runner-patch-reboot

## Overview

Wait for the host to settle before dispatching; do not retry on EOF. The
design basis is the investigation's recommended fix (P0-A, P0-B, P1); P2 is
deferred. Pure decisions live in `build_planner.py`, I/O in
`build_dispatcher.py`, matching the existing split.

**Bug condition.** An agent command is sent to a host on which an SSM
association is still running (the patch run that will reboot it), or the
bootstrap is cut short by that reboot.

## P0-A — host-settled gate (ephemeral)

- `build_dispatcher.runner_association_statuses(instance_id)` reads
  `DescribeInstanceAssociationsStatus` (every page, `NextToken` loop that
  stops on any non-fresh token) and returns `[(name, status)]` (document
  name, falling back to association name/id), or `None` on `ClientError`.
- `build_planner.decide_host_settled(job, statuses, now)` returns a
  `HostSettledDecision` (`readiness`, `running`, `statuses_read`,
  `log_path`, `deadline`, `timeout_minutes`, `error`). `Pending` and
  `InProgress` (case-insensitive) block; everything else never does.
  `None` waits. Budget, deadline and the strict `now > deadline` boundary
  are `decide_runner_readiness`'s; a missing `dispatched_at` waits.
  A settled host is READY even past the deadline.
- Placement in `provision_ephemeral`: after the marker decision returns
  READY, before `bootstrap.marker_at` is recorded, the preflight and the
  attempt claim. WAIT does nothing this tick (logged); TIMEOUT goes through
  `fail_bootstrap_timeout` (body unchanged; its annotation now names both
  decision types, which both carry `error` and `log_path`), which
  terminates the runner.
- READY records `bootstrap = {marker_at, log_path, settled_at,
  associations: [{name, status}]}` before the send.
- Why every association: the account-managed set changes without notice;
  during exit 194 -> reboot -> resume the patch association stays
  `InProgress` until the post-reboot resume finishes, covering the reboot
  and the agent self-update.
- IAM: a separate read-only statement
  (`ssm:DescribeInstanceAssociationsStatus`, `*`) in `grantSsmCommands`,
  next to `DescribeInstanceInformation`. A separate statement keeps the
  existing "reconciliation read statements grant only read actions"
  allowlist test unchanged. `grantSsmCommands` also serves the BuildJobs
  role, which therefore gets the same read-only action.

## P0-B — bootstrap that survives the reboot and the dpkg lock

- `runner_bootstrap_user_data` now opens with
  `runner_bootstrap_reentry_commands()`:
  1. `[ -f <marker> ] && exit 0` — the first statement;
  2. `if { exec 9>><lock>; } 2>/dev/null; then flock 9; fi` — a blocking
     lock on `/var/lock/dda-runner-bootstrap.lock` (an unopenable lock file
     skips the lock, never the bootstrap), then the guard again so a run
     that waited behind another exits once that one wrote the marker;
  3. `install -D -m 0700 "$0" /var/lib/cloud/scripts/per-boot/dda-runner-bootstrap.sh`
     unless `$0` already is that path, tolerated.
  The rest of the script is unchanged and the marker write stays last.
- Mechanism: the user data IS the generated bootstrap script, so it writes
  a root-owned 0700 copy of itself (the file cloud-init executes, `$0`) to
  the per-boot path and then runs once as cloud-init's user data. That is
  "write the script, then run it once" with one script text, rather than a
  here-doc wrapper around a second copy; the text at the per-boot path is
  byte-identical to the generated script (tested).
- cloud-init order (verified in the `cloud-init 26.1` packages of Ubuntu
  22.04 and 24.04): `cloud_final_modules` runs `scripts_per_boot` (frequency
  PER_ALWAYS) before `scripts_user` (PER_INSTANCE), and the per-instance
  semaphore is written before the module runs. So on first boot the copy is
  not there yet when per-boot scripts run and the user data runs it once; on
  a reboot before the marker the user data is NOT re-run but the per-boot
  copy is, and after the marker every boot exits at the guard.
- Re-run idempotency: the sync is clone-if-absent plus fetch and forced
  checkout; `setup-build-server.sh` guards its installs (`command -v`,
  `grep -q`) and its apt/snap steps are repeatable; the marker is written
  last. Executed evidence: `TestPerBootReentryIsIdempotent`.
- Lock waits: `-o DPkg::Lock::Timeout=600` on every `apt-get` of the runner
  user data, `build_fleet.USER_DATA_BODY` and `setup-build-server.sh`
  (placed before the sub-command). It makes `install`/`remove` wait for the
  dpkg frontend lock; apt does not apply it to the lists lock of
  `apt-get update` (verified), which stays tolerated as before.
- Ships with P0-A: P0-A's longer wait makes a reboot during the bootstrap
  more likely.

## P1 — advisory deferral for dedicated servers

- `build_planner.decide_dedicated_settle(job, statuses, now)` returns a
  `DedicatedSettleDecision` (`action` START/DEFER, `running`,
  `statuses_read`, `deferral_started_at`, `advisory`). `None` -> START with
  a fail-open advisory. Nothing running -> START (advisory only when a
  deferral is ending). Running and `now <= start + cap` -> DEFER (the start
  is `now` on the first such check). Running past the cap -> START with an
  advisory naming the associations. No path fails a job.
- Cap: `DEDICATED_SETTLE_DEFERRAL_CAP_MINUTES = 30`, next to
  `DEFAULT_BOOTSTRAP_TIMEOUT_MINUTES`, strict boundary like the module's
  other deadlines.
- Placement in `verify_and_start_dedicated`: after the pgrep verification,
  the dedicated bootstrap policy and the preflight, before the attempt
  claim — the last gate before a start, so an invalid contract fails
  without reading anything.
- A deferral writes `deferred_at` (the 5-minute re-verification cadence)
  and `host_settle = {deferral_started_at, checked_at, running, advisory}`,
  keeping the server allocation. Writing `deferred_at` matters: it is the
  deferral activity the exit-75 lock-deferral stall backstop reads, so a
  requeued dedicated job waiting here is never failed as stalled. With the
  5-minute cadence the worst-case wait is the cap plus one interval.
- A START with an advisory records `host_settle` (with
  `deferral_started_at: None`) in the same conditional queued -> building
  write as the attempt claim, so a later requeue starts a fresh deferral.
  A plain settled start writes nothing new.

## Risks and Limits

- An association stuck `Pending` fails ephemeral jobs at the budget; the
  error names it.
- Dedicated builds already running when a patch run starts stay exposed.
- `apt-get update` is not covered by the lock option; a reboot mid-`dpkg`
  may need `dpkg --configure -a` (not handled).
- A pre-baked AMI that carries the marker would skip the bootstrap at the
  guard (the agent preamble still syncs the source). The incident's runners
  used stock Canonical AMIs.

## Testing

Pure decisions: `test_runner_settle_unit.py`. Gate wiring and per-boot
execution: `test_bootstrap_gate_property.py`. Tick integration (P0-A and
P1 scenarios): `test_dispatcher_tick_integration.py`. Static: the setup
script lock-wait test, the build-fleet-stack IAM assertion, re-recorded
byte oracles. Fakes of sibling suites answer the new read as settled.

## Deploy Impact

The Lambda and IAM changes ship with a Portal deploy; never while a
component build runs, then move `cdk.out` aside and re-run the guard pair.
`setup-build-server.sh` reaches runners through the synced ref, so it must
be on the branch being built; existing dedicated servers pick it up only if
setup is re-run. No device or LocalServer component change.

