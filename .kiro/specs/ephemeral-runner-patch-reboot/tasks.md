# Implementation Plan

## Overview

Ship P0-A, P0-B and P1 together (owner-approved, Ryan Vanderwerf,
2026-10-07). P2 stays deferred. Evidence for every completed task is in
`verification-notes.md`.

## Tasks

- [x] 1. Baseline the untouched worktree
  - Run the portal build suites, the infrastructure jest suites and the
    security gates before any edit; record the pre-existing failures and
    the exact commands that worked.
  - _Requirements: 3.1-3.4_

- [x] 2. P0-A pure decision
  - `build_planner.decide_host_settled` and `running_associations`, next to
    `decide_runner_readiness`, same budget/deadline/boundary.
  - New `test_runner_settle_unit.py`: Pending/InProgress wait (any case),
    Success/Failed/Skipped/empty ready, `None` waits, at the deadline WAIT,
    strictly past TIMEOUT naming the associations.
  - _Requirements: 2.1, 2.2, 2.4_

- [x] 3. P0-A dispatcher wiring and IAM
  - `runner_association_statuses` (paginated, `None` on `ClientError`);
    gate between marker READY and the `marker_at` record/claim; record
    `bootstrap.settled_at` and `bootstrap.associations`; TIMEOUT through
    `fail_bootstrap_timeout`.
  - `ssm:DescribeInstanceAssociationsStatus` (read-only, `*`) in
    `grantSsmCommands`; jest assertion on the dispatcher role.
  - Gate properties in `test_bootstrap_gate_property.py`; tick scenario in
    `test_dispatcher_tick_integration.py` (InProgress -> no send; Success
    -> one send; past budget -> `BOOTSTRAP_TIMEOUT` naming it, runner
    terminated).
  - _Requirements: 2.1-2.4, 3.1, 3.2_

- [x] 4. P0-B per-boot re-entry and dpkg lock waits
  - `runner_bootstrap_reentry_commands`: marker guard first, own flock,
    guard again, root 0700 per-boot self-install; marker write still last.
  - `-o DPkg::Lock::Timeout=600` on every `apt-get` in the runner user
    data, `build_fleet.USER_DATA_BODY` and `setup-build-server.sh`.
  - Confirm the cloud-init module order (22.04, 24.04) and the re-run
    idempotency; executed sandbox tests (twice = no-op; reboot partway
    re-runs to completion); setup-script lock-wait test; re-recorded byte
    oracles.
  - _Requirements: 2.5, 2.6, 3.4_

- [x] 5. P1 dedicated advisory deferral
  - `build_planner.decide_dedicated_settle` with the 30-minute cap
    constant; wired after the preflight, before the claim; `deferred_at`
    plus `host_settle` on a deferral; advisory with the dispatch.
  - Tick scenarios: defers while InProgress (lock kept, follower queued),
    dispatches after Success, dispatches after the cap with an advisory,
    does not defer when the read errors.
  - _Requirements: 2.7, 2.8, 3.3_

- [x] 6. Keep sibling suites unchanged
  - Fakes and moto-based suites answer the new read as settled; the
    no-live-validation contract allow-lists it as a read.
  - _Requirements: 3.1-3.4_

- [x] 7. Verification
  - Portal build suites, infrastructure jest (build-fleet-stack and full),
    guard pair, IAM CDK synth gate, baselines scan, `bash -n`, shellcheck,
    public-repo grep. Results in `verification-notes.md`.

- [x] 8. Commit on `spec/ephemeral-runner-patch-reboot`

- [x] 9. Rebase, push and merge (later workflow step)

- [x] 10. Portal deploy (later step): not while a component build runs;
  afterwards move `cdk.out` aside and re-run the guard pair. Record the
  deploy evidence in `verification-notes.md`.

- [ ] 11. Live check (later step): an ephemeral build on a fresh runner
  waits for the patch association, then completes without a reboot.

## Notes

- Out of scope: P2, the account's patch association, capacity errors on
  m6i.4xlarge, LocalServer/device code.

