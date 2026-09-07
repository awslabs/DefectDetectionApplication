# Implementation Plan

## Overview

Portal build tests live in `test/backend-test/portal_builds/` (NOT
`edge-cv-portal/backend/tests`). Run with pytest from the repo root, e.g.
`python3 -m pytest test/backend-test/portal_builds/test_bootstrap_zip_exploration.py -q -p no:cacheprovider`.
Property tests use hypothesis. Follow the sibling files' import
discipline (pop `BUILD_REPO_URL` / dispatcher env before importing the
function modules — see `test_source_selection_preservation.py` header and
`test_source_dir_alignment_property.py`).

## Tasks

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Rendered bootstrap never installs zip
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples showing every rendered user-data lacks zip/unzip
  - **Scoped PBT Approach**: hypothesis over (repo_dir, source_ref) inputs (including None/'' and ref-bearing values), plus the concrete incident-shaped case: `render_user_data('https://example.invalid/dda.git')` with defaults — the exact path `run_fleet_instance` uses for POST /build-servers launches like srv-aac90870 / i-07c2ca92f3a526c93
  - New file `test/backend-test/portal_builds/test_bootstrap_zip_exploration.py`
  - For each rendered text, locate the ROOT apt-get install line(s) (the prologue lines run as root, before the sudo/here-doc body — do NOT count `setup-build-server.sh`'s own apt line, which is ref-dependent and failure-tolerant and demonstrably did not protect the live server) and assert its package list contains `zip` and `unzip`
  - Cover ALL render paths: `build_fleet.render_user_data(...)`, the module-level `build_fleet.USER_DATA_TEMPLATE`, and `build_dispatcher.runner_bootstrap_user_data(job, repo_dir)` for a job with and without a source_ref (mock `BUILD_REPO_URL` as `test_run_as_ubuntu_unit.py` does; skip-assert the empty-string no-repo-URL case, which legitimately renders nothing)
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS on every render path (this is correct - it proves the bug: the only root apt line is `apt-get install -y git`)
  - Document counterexamples found (the actual apt line text per path) in the test docstring or a counterexamples note
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.3, 2.1, 2.3_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Bootstrap text unchanged outside the apt package list
  - **IMPORTANT**: Follow observation-first methodology - record what the UNFIXED generators emit, then pin it
  - New file `test/backend-test/portal_builds/test_bootstrap_zip_preservation.py`
  - Observe on UNFIXED code, for hypothesis-generated (repo_url, repo_dir, source_ref):
    - `render_user_data` output normalized by mapping the root apt-get install line to a canonical token — pin every OTHER line: shebang/`set -x`, non-fatal log redirect (`BOOTSTRAP_LOG=...`), the `git clone {repo_url} {repo_dir}` line, the Source_Sync here-doc byte-equal to `build_source.source_sync_commands` output, the `setup-build-server.sh` invocation, and the tolerated `touch <marker>` as the LAST statement
    - `USER_DATA_TEMPLATE` still has `{repo_url}` as its ONLY unbound placeholder (`.format(repo_url=...)` succeeds; no other `{...}` field remains)
    - rendered text passes `bash -n` (reuse the `test_bootstrap_gate_property.py` syntax-check approach)
    - same normalized pinning for `runner_bootstrap_user_data` (root prologue order: log redirect → HOME export → apt line → parent-prepare/ownership-heal → sudo body → classified sync exits → marker last)
  - Also run the EXISTING adjacent suites once on unfixed code to record a green baseline: `test_bootstrap_gate_property.py`, `test_source_dir_alignment_property.py`, `test_run_as_ubuntu_unit.py`, `test_source_selection_preservation.py`, `test_jp7_mixed_batch_tick_integration.py`
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline the fix must preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.5_

- [x] 3. Fix: install zip/unzip in both bootstrap generators

  - [x] 3.1 Implement the fix
    - `edge-cv-portal/backend/functions/build_fleet.py` `USER_DATA_BODY` (~line 305): change `apt-get install -y git` to `apt-get install -y git zip unzip`; one edit covers `USER_DATA_TEMPLATE`, `render_user_data`, and the POST /build-servers launch path for BOTH ubuntu_flavor values (pro/standard share the same render path — flavor only selects the AMI)
    - `edge-cv-portal/backend/functions/build_dispatcher.py` `runner_bootstrap_user_data` (~line 1176): change `apt-get update -y && apt-get install -y git` to `... git zip unzip`
    - Update the `USER_DATA_BODY` docstring: note zip/unzip are installed root-side because `build-custom.sh`'s packaging step (ZIP_MEMBERS zip + `zip -T`) requires them, Ubuntu cloud images don't ship them, and the synced ref's `setup-build-server.sh` apt line is failure-tolerant and ref-dependent (the 2026-09-07 incident); clarify the "preinstalled on Ubuntu 22.04" sentence refers to the SSM agent
    - Update the FROZEN byte-level oracle `frozen_runner_bootstrap` in `test/backend-test/portal_builds/test_jp7_mixed_batch_tick_integration.py` to the new dispatcher apt line — a CONSCIOUS re-record, noted in the commit; do not weaken the oracle
    - _Bug_Condition: isBugCondition(X) — root apt line of any rendered bootstrap lacks zip_
    - _Expected_Behavior: every rendered bootstrap's root apt line installs zip and unzip_
    - _Preservation: all other bootstrap text byte-identical; {repo_url} sole placeholder; marker-last; bash -n clean_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3_

  - [x] 3.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Rendered bootstrap installs zip
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - Run `test_bootstrap_zip_exploration.py`
    - **EXPECTED OUTCOME**: Test PASSES on all render paths (confirms bug is fixed)
    - _Requirements: 2.1, 2.3_

  - [x] 3.3 Verify preservation tests still pass
    - **Property 2: Preservation** - Bootstrap text unchanged outside the apt package list
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run `test_bootstrap_zip_preservation.py` plus the existing adjacent suites baselined in task 2 (including the re-recorded `test_jp7_mixed_batch_tick_integration.py`)
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.5_

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the full portal_builds suite: `python3 -m pytest test/backend-test/portal_builds -q -p no:cacheprovider`
  - Ensure all tests pass, ask the user if questions arise

- [x] 5. Deploy and verify live (Lambda-managed asset)
  - The fix lands in BuildFleetHandler (EdgeCVPortalBuildFleetStack) and the dispatcher handler; it is NOT live until a portal infrastructure deploy runs
  - Honor `.kiro/steering/builds.md`: confirm no component build is running (`pgrep -af "gdk component build"` / `pgrep -af "build-custom.sh"`) before deploying; NEVER deploy while one runs
  - Run `edge-cv-portal/deploy-infrastructure.sh`
  - After the deploy: move the regenerated `edge-cv-portal/infrastructure/cdk.out` aside (`mv cdk.out cdk.out.bak-$(date +%Y%m%dT%H%M%SZ)`) and re-run the preservation guard suite so the next component build's security gate stays green
  - Live verification: launch a fresh build server via POST /build-servers and confirm via SSM that `command -v zip && command -v unzip` succeeds on the new instance (or, at minimum, invoke the deployed handler path and assert the rendered user-data contains the new apt line); already-running servers are out of scope (SSM-mitigated)
  - _Requirements: 2.1, 2.2, 3.4_

## Notes

- The Task 1 exploration test MUST fail on unfixed code before any fix is implemented — the failure is what confirms the bug exists. Do not fix the test or the code at that stage.
- The frozen byte-level oracle update in task 3.1 (`frozen_runner_bootstrap` in `test_jp7_mixed_batch_tick_integration.py`) is a conscious re-record to the new apt line, not a weakening of the oracle.
- The deploy in task 5 must honor `.kiro/steering/builds.md`: confirm no component build is running before deploying, and move the regenerated `cdk.out` aside afterward so the next build's security gate stays green.
