# Implementation Plan: JP7 Ephemeral Runner Provisioning (bugfix)

## Overview

This plan implements the approved `bugfix.md` and `design.md` through the bug-condition workflow, covering **both execution modes**: ephemeral (target-aware OS-release selection and fail-closed AMI resolution) and dedicated (noble bootstrap deltas in the one shared seam, plus the JP7 capability gate).

Tasks 1 and 2 are standalone pre-fix tests. Task 1 MUST FAIL on unfixed code (its failures are the counterexamples that confirm the bug condition); task 2 MUST PASS on unfixed code (it freezes the preservation oracles). Neither baseline may be rewritten after implementation — task 1 is re-run unchanged in 3.8 and task 2 unchanged in 3.9.

Property numbering follows `design.md` Correctness Properties 1–8 throughout: Properties 1, 2, 5, 6 are bug-condition (fix-checking) properties; Properties 3, 4, 7, 8 are preservation properties. Task 1 encodes Property 1's expectation (and probes the surfaces of Properties 2, 5, 6); task 2 freezes the oracles Properties 3, 4, 7, 8 later assert against.

Every task is a repo edit or a test run. All tests are offline (stubbed `boto3`/`shared_utils`, existing `test/backend-test/portal_builds/` harness conventions); no task except the optional task 10 launches an instance, sends an SSM command, or runs a build. All test commands are finite/non-watch invocations. Property-based test tasks must be run with the execution warning: `This test run contains property-based tests and may generate/shrink counterexamples.`

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1", "2"] },
    { "id": 1, "tasks": ["3.1"] },
    { "id": 2, "tasks": ["3.2", "3.3", "3.4", "3.7"] },
    { "id": 3, "tasks": ["3.5"] },
    { "id": 4, "tasks": ["3.6"] },
    { "id": 5, "tasks": ["3.8", "3.9"] },
    { "id": 6, "tasks": ["4"] },
    { "id": 7, "tasks": ["5.1", "5.2", "5.3", "5.4", "5.5", "5.6", "5.7", "5.8"] },
    { "id": 8, "tasks": ["6.1", "6.2", "6.3", "6.4"] },
    { "id": 9, "tasks": ["7"] },
    { "id": 10, "tasks": ["8.1", "8.2", "8.3", "8.4"] },
    { "id": 11, "tasks": ["9"] },
    { "id": 12, "tasks": ["10.1"] },
    { "id": 13, "tasks": ["10.2"] }
  ]
}
```

- Wave 0 runs on UNFIXED code only: task 1 must fail, task 2 must pass. No production edit may precede them.
- Wave 1 lands the domain OS-release table plus accessor — the single derivation both the planner (3.3) and the capability gate (3.2) consume.
- Wave 2 is independent file work: the gate in `build_domain.py`, the plan field in `build_planner.py`, the noble constants in `build_dispatcher.py`, and the shared script deltas in `setup-build-server.sh`.
- Waves 3–4 are strictly ordered inside `build_dispatcher.py`: `resolve_ami` needs the constants, and the call-site/record wiring needs `resolve_ami` and `plan.os_release`.
- Wave 5 re-runs the two frozen pre-fix baselines (now expected to flip to pass, and to stay passing).
- Waves 7–8 add fix-checking and preservation property tests plus the enumerated unit tests; wave 10 adds the dispatcher-tick and request-flow integration tests.
- Waves 12–13 are the optional live verifications, in the user's stated order: dedicated 24.04 build machine first (docker layer caches persist, faster iteration), then the ephemeral path with cold caches.

## Tasks

- [x] 1. Write and run bug-condition exploration tests on unfixed code
  - **Property 1: Bug Condition** - Wrong-OS JP7 provisioning, missing capability gate, noble-incompatible shared bootstrap
  - **CRITICAL**: Write and run this task before any production edit. The test MUST FAIL on unfixed code — the failure is what confirms the bug exists. **DO NOT attempt to fix the test or the code when it fails.**
  - **NOTE**: This test encodes the post-fix expected behavior, so it validates the fix when it passes in task 3.8. Do not weaken its assertions afterwards.
  - **GOAL**: Surface counterexamples demonstrating that JP7 is planned/provisioned as a jammy host and that no layer screens an incapable dedicated host.
  - **Scoped PBT Approach**: the defect is deterministic, so scope each property to the concrete failing cases (build_target `JP7`; server `ubuntu_version` `'22.04'` and field-absent; the checked-in `setup-build-server.sh` text) while keeping the surrounding inputs (config_snapshot, source ref, repo dir, region) generated
  - Create `test/backend-test/portal_builds/test_jp7_ephemeral_provisioning_exploration.py` using the suite's stubbed-`boto3`/`shared_utils` harness conventions, with five cases from design's Exploratory Bug Condition Checking:
  - (a) **Plan carries no OS release**: `plan_runner(jp7_ephemeral_job)` — assert the plan distinguishes JP7's required host OS (`'24.04'`) from JP5's (`'22.04'`). Fails on unfixed code: `RunnerPlan` has no such field (1.3)
  - (b) **Jammy AMI selected for JP7**: with a recording SSM stub and no env overrides, resolve the AMI for a JP7 plan — assert the requested parameter path contains `24.04`. Fails on unfixed code: the 22.04 arm64 parameter is read (1.1, 1.2)
  - (c) **Jammy env override applied to JP7**: with `BUILD_ARM64_AMI_ID` set to a jammy pin, resolve for JP7 — assert the jammy pin is NOT returned. Fails on unfixed code (1.1)
  - (d) **No dedicated capability gate**: `validate_build_request` for JP7 + dedicated selecting a running arm64 server with `ubuntu_version='22.04'`, and again with the field ABSENT (pre-ec1dc38 record) — assert rejection naming the missing Ubuntu 24.04 arm64 capability and the server's actual release, with no job record created. Fails on unfixed code: both requests are accepted (1.7, 1.8)
  - (e) **Noble-incompatible shared bootstrap**: static assertions on the checked-in `setup-build-server.sh` — `sudo pip3 install awscli`, `sudo pip3 install --no-compile 'botocore[crt]'`, and `pip3 install --user git+...aws-greengrass-gdk-cli` each carry a PEP 668 flag, and the docker-compose section provides the shim rather than knowing only the snap install; plus `build_fleet.render_user_data(...)` for `ubuntu_version='24.04'` reaches the noble deltas rather than rendering a release-blind plain script run. Fails on unfixed code (1.5, 1.6)
  - Run only this file, finite/non-watch, with the property-test warning; record the shrunk counterexamples and the observed unfixed outputs (requested SSM parameter path, returned AMI id, accepted-validation result, the three unflagged pip command lines, the identical 22.04/24.04 rendered user-data) in `test/backend-test/portal_builds/jp7_ephemeral_provisioning_counterexamples.md`
  - **EXPECTED OUTCOME**: Test FAILS (this is correct — it proves the bug exists). Mark complete only after the failures are reproduced and documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8_

- [x] 2. Write and run observation-first preservation baseline tests on unfixed code (BEFORE implementing the fix)
  - **Property 3: Preservation** - Non-JP7 planning/AMI resolution, both bootstrap seams, and dedicated validation outcomes
  - **IMPORTANT**: Follow the observation-first methodology — run the UNFIXED code on inputs where `isBugCondition` is false, record the ACTUAL outputs, then encode them as frozen oracles in the suite's `FROZEN_MATRIX` re-spelling style (do not import the pre-fix code). Do not rebaseline this oracle after implementation
  - Create `test/backend-test/portal_builds/test_jp7_ephemeral_preservation.py` with property-based tests (Hypothesis, `@settings(max_examples=100, deadline=None)` minimum) capturing the observed behavior:
  - Observe and freeze `plan_runner` field outputs (arch, instance_type, volume_size_gb, spot, status) for generated JP5/JP6/AMD64/AMD64_NVIDIA ephemeral jobs with generated `config_snapshot`s (3.4)
  - Observe and freeze `resolve_ami` resolution ORDER with a recording stub across all `BUILD_ARM64_AMI_ID`/`BUILD_X86_64_AMI_ID` override configurations: env override → per-container cache → the exact per-architecture 22.04 SSM parameter path, including the cache-hit behavior on a second call (3.1, 3.2)
  - Observe and freeze `runner_bootstrap_user_data` output text for generated jobs of ALL supported targets, refs, repo dirs, and regions (byte-level oracle, including the trailing root-written Bootstrap_Marker statement) (3.3)
  - Observe and freeze `build_fleet.render_user_data` output text for generated repository/dir/ref inputs at `ubuntu_version` 22.04 and absent (3.8)
  - Observe and freeze `validate_build_request` accept/reject outcomes and error rule ids over generated requests and fleets for every target/mode combination other than JP7+dedicated, including JP5/JP6 dedicated against servers whose `ubuntu_version` is `'22.04'`, `'24.04'`, an arbitrary other string, and absent (3.5, 3.9)
  - Observe and freeze the jammy command sequence of `setup-build-server.sh`: the exact three pip install command lines and the snap `docker-compose` branch (3.8)
  - Run this file plus the existing `test/backend-test/portal_builds/` suites it neighbors, finite/non-watch, with the property-test warning
  - **EXPECTED OUTCOME**: Tests PASS on UNFIXED code (this confirms the baseline behavior to preserve). Mark complete only after they are written, run, and passing
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.8, 3.9_

- [x] 3. Fix for JP7 builds failing in both execution modes (wrong-OS ephemeral runner, incapable dedicated host)

  - [x] 3.1 Add the per-target OS-release attribute and accessor to `build_domain.py`
    - Add `OS_RELEASE_JAMMY = '22.04'` and `OS_RELEASE_NOBLE = '24.04'`; give every `BUILD_TARGETS` entry a `'required_os_release'` key — `22.04` for JP5, JP6, AMD64, AMD64_NVIDIA; `24.04` for JP7
    - Add `required_os_release_for_target(target)` delegating to `target_definition` so unsupported targets keep raising `ValueError`; this accessor is the ONLY API the rest of the fix consumes
    - Leave every existing reader unchanged (`create_build_jobs`, `retry_clone`, dispatch preflight select named keys); if the suite surfaces an exact-shape assertion on `target_definition`'s dict, fall back to a separate module-level mapping behind the same accessor
    - _Bug_Condition: `isBugCondition(job)` where `job.build_target = 'JP7'` — the domain table carries no OS-release dimension to derive from_
    - _Expected_Behavior: `required_os_release_for_target('JP7') = '24.04'`, every other supported target `'22.04'` (design Property 1's frozen oracle)_
    - _Preservation: existing `BUILD_TARGETS` keys and every downstream reader unchanged (3.4, 3.5)_
    - _Requirements: 2.1, 2.2, 2.7_

  - [x] 3.2 Add the JP7 dedicated capability gate to `validate_build_request`
    - Add rule id `RULE_SERVER_OS_RELEASE_MISMATCH`; inside the existing dedicated-server validation block, after the arch-mismatch check with the same found-and-running record in hand, for each supported selected target whose `required_os_release_for_target(target)` is `'24.04'`, compare against `server.get('ubuntu_version') or '22.04'` so a pre-ec1dc38 record with no field is treated as the 22.04 host it is
    - On mismatch append a rejection naming both the missing capability and the actual release, e.g. "Dedicated_Build_Server '<id>' runs Ubuntu <actual>, but Build_Target JP7 requires an Ubuntu 24.04 arm64 build host. Select a 24.04 arm64 server (or use the ephemeral execution mode)."
    - Compose with, never mask, the existing rules: not-found, not-running, and arch-mismatch rejections still fire and are still reported alongside the new one; targets requiring `'22.04'` impose NO release constraint; `plan_dedicated_dispatch` and the rest of the dedicated machinery are NOT touched
    - Rejection is at validation time, so no Build_Job record is created (jetpack7-support Req 6.4/6.5 fail-closed semantics)
    - _Bug_Condition: `isBugCondition(job)` where `execution_mode = 'dedicated'` AND `recordedRelease(selectedServer) != '24.04'`_
    - _Expected_Behavior: accept iff exists AND running AND arm64 AND recorded release `'24.04'`; otherwise reject with `RULE_SERVER_OS_RELEASE_MISMATCH` naming capability and actual release (design Property 5)_
    - _Preservation: every non-JP7 outcome and every existing rule unchanged; JP5/JP6 dedicated accepted on any server release (3.5, 3.6, 3.9; design Property 8)_
    - _Requirements: 2.7, 3.5, 3.6, 3.9_

  - [x] 3.3 Add `os_release` to `RunnerPlan` and derive it in `plan_runner`
    - Append `os_release: str = '22.04'` as the LAST field of `RunnerPlan` so existing positional constructions, field access, and field-wise comparisons are unaffected
    - Set it in `plan_runner` from `build_domain.required_os_release_for_target(job['build_target'])`; for every existing target the derived value equals the default
    - Change nothing else in the planner: arch, instance type, volume size, spot flag, and status derivation stay as they are
    - _Bug_Condition: `plan_runner` for a JP7 ephemeral job produces a plan carrying only arm64, giving the dispatcher no release to resolve on (defect clause 1.3)_
    - _Expected_Behavior: `(plan.os_release, plan.arch) = ('24.04', 'arm64')` for JP7; `('22.04', required_arch)` for every other target (design Property 1)_
    - _Preservation: identical planner fields for non-JP7 jobs (3.4; design Property 3)_
    - _Requirements: 2.2, 3.4_

  - [x] 3.4 Add the noble AMI constants to `build_dispatcher.py`
    - `ARM64_NOBLE_AMI_SSM_PARAMETER`, env-overridable, default `/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id` — note the `ebs-gp3` segment (the noble tree differs from the jammy `ebs-gp2` path), following `build_fleet.resolve_ubuntu_ami`'s verified conventions
    - `CANONICAL_OWNER_ID = '099720109477'` and the noble arm64 DescribeImages name filter `ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*`
    - `BUILD_ARM64_NOBLE_AMI_ID` — a NEW optional override scoped to noble ONLY; document on the constants that `BUILD_ARM64_AMI_ID`/`BUILD_X86_64_AMI_ID` remain scoped to 22.04 because they are jammy pins today and letting one silently override a noble runner would reintroduce this bug through configuration
    - _Bug_Condition: the dispatcher has no noble mapping at all — only two 22.04 parameters and two jammy overrides (defect clause 1.1)_
    - _Expected_Behavior: the noble arm64 conventions of design Property 1 are available to resolution (ebs-gp3 parameter, hvm-ssd-gp3 filter, Canonical owner)_
    - _Preservation: existing jammy constants and override names byte-unchanged (3.1, 3.2)_
    - _Requirements: 2.1, 3.2_

  - [x] 3.5 Widen `resolve_ami` to `resolve_ami(arch, os_release='22.04')` with a fail-closed default
    - `os_release == '22.04'`: today's body unchanged — `BUILD_ARM64_AMI_ID`/`BUILD_X86_64_AMI_ID` override, then `_AMI_CACHE[arch]`, then the existing 22.04 SSM parameter, cache keyed exactly as today
    - `os_release == '24.04'` and `arch == 'arm64'`: `BUILD_ARM64_NOBLE_AMI_ID` override, then a distinct noble cache key (e.g. `'24.04/arm64'`) that can never collide with the jammy keys, then the noble SSM parameter, then the Canonical-owner DescribeImages fallback selecting the newest image by CreationDate, caching the result; a resolution finding no image raises with a diagnostic naming release and architecture
    - Any other pairing: `raise ValueError(f"No Ubuntu {os_release} AMI mapping for architecture {arch} on the ephemeral runner path")` — raised BEFORE any AWS call (no SSM GetParameter, no DescribeImages). `ValueError` is deliberate, not `build_fleet`'s `RuntimeError`: `provision_ephemeral` already catches `(ClientError, ValueError)` and routes to `fail_provisioning`, so the one job fails with `ERROR_PROVISIONING_FAILED` carrying the pairing instead of the dispatcher tick crashing
    - Jammy overrides must not apply to noble resolution, and the noble override must not apply to jammy resolution
    - _Bug_Condition: `resolve_ami('arm64')` returns a jammy AMI for a JP7 job; unmapped pairings have no defined behavior_
    - _Expected_Behavior: noble resolution via the ebs-gp3 parameter / hvm-ssd-gp3 fallback, and fail-closed `ValueError` naming release and arch with zero AWS calls (design Properties 1, 2)_
    - _Preservation: identical AMI id through the identical resolution order and cache semantics for 22.04 (3.1, 3.2; design Property 3)_
    - _Requirements: 2.1, 2.3, 3.1, 3.2_

  - [x] 3.6 Wire the plan's release through provisioning and leave the bootstrap user-data untouched
    - `run_runner_instance`: `'ImageId': resolve_ami(plan.arch, plan.os_release)`
    - `provision_ephemeral`: record `'os_release': plan.os_release` on the runner record as an additive diagnostic field alongside `arch`/`instance_type`; no control-flow change, and the existing `(ClientError, ValueError)` → `fail_provisioning` path stays as-is
    - `runner_bootstrap_user_data` is deliberately UNCHANGED — no release parameterization, no noble prologue. The deltas travel in `setup-build-server.sh` (3.7), which the emitted build-user body already executes; a dispatcher-side prologue would be the second divergent copy Req 2.8 prohibits
    - Instance sizing, profile, tags, readiness gating, and the Bootstrap_Marker probe are release-agnostic and unchanged
    - _Bug_Condition: a JP7 ephemeral job is provisioned from the jammy AMI (defect clauses 1.1, 1.2)_
    - _Expected_Behavior: the JP7 runner launches from the noble arm64 AMI, its runner record carries `os_release='24.04'`, and unmapped pairings fail just that job (design Properties 1, 2)_
    - _Preservation: `runner_bootstrap_user_data` byte-identical for ALL targets (3.3; design Property 4)_
    - _Requirements: 2.1, 2.3, 2.4, 3.3_

  - [x] 3.7 Add the release-keyed noble deltas to `setup-build-server.sh` (the single shared seam)
    - **Detection**: near the top, detect the host release once — `OS_RELEASE=$(. /etc/os-release && echo "$VERSION_ID")` with an `lsb_release -rs` fallback (the mechanism the script's Python-3.11 block already uses) — and derive `PIP_BREAK_FLAG='--break-system-packages'` when `OS_RELEASE` is `24.04`, empty otherwise
    - **docker-compose**: branch the existing `if ! command -v docker-compose` block on the release — on 24.04 write `/usr/local/bin/docker-compose` as a shim (`#!/bin/sh` + `exec docker compose "$@"`) and `chmod +x` it, never reaching the snap install on that branch; on 22.04 the snap branch runs verbatim as today
    - **PEP 668 installs**: add `$PIP_BREAK_FLAG` to exactly the three affected invocations — `sudo pip3 install awscli`, `sudo pip3 install --no-compile 'botocore[crt]'`, and `pip3 install --user git+...aws-greengrass-gdk-cli...`. On 22.04 the flag expands to nothing so the command lines are today's exactly (jammy's pip would reject the unknown option, which is why it must be release-conditioned rather than unconditional)
    - Keep the script's existing `run_cmd`/error-summary conventions for every new statement; add NO delta text to `build_fleet.USER_DATA_BODY` or `runner_bootstrap_user_data` — both already execute this script, so both modes inherit one implementation
    - Verify syntax with `bash -n setup-build-server.sh`
    - _Bug_Condition: `isBugCondition(job)` where the selected 24.04 server has `NOT nobleDeltasApplied` — the release-blind bootstrap runs a noble-incompatible script (defect clauses 1.4, 1.5, 1.6)_
    - _Expected_Behavior: on a detected 24.04 host the shim is written and all three pip installs carry `--break-system-packages`, reached identically from both seams, so a portal-launched 24.04 server is JP7-build-capable with no manual step (design Property 6)_
    - _Preservation: on a detected 22.04 host the effective command sequence is today's — snap branch reached as today, flag expanding empty (3.3, 3.8; design Property 7)_
    - _Requirements: 2.4, 2.6, 2.8, 3.8_

  - [x] 3.8 Verify the bug-condition exploration test now passes
    - **Property 1: Expected Behavior** - Wrong-OS JP7 provisioning, missing capability gate, noble-incompatible shared bootstrap
    - **IMPORTANT**: Re-run the SAME test file from task 1 — do NOT write a new test and do NOT weaken any assertion. That file encodes the expected behavior, so its passing is what confirms the fix
    - Run `test/backend-test/portal_builds/test_jp7_ephemeral_provisioning_exploration.py`, finite/non-watch, with the property-test warning
    - **EXPECTED OUTCOME**: Test PASSES (confirms the bug is fixed in both modes — noble plan and AMI, capability rejection, noble deltas present in the shared script)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.6, 2.7, 2.8_

  - [x] 3.9 Verify the preservation baseline tests still pass
    - **Property 3: Preservation** - Non-JP7 planning/AMI resolution, both bootstrap seams, and dedicated validation outcomes
    - **IMPORTANT**: Re-run the SAME test file from task 2 — do NOT write new tests and do NOT rebaseline the frozen oracles
    - Run `test/backend-test/portal_builds/test_jp7_ephemeral_preservation.py` plus the neighboring existing suites it was run with in task 2
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions to non-JP7 behavior or 22.04 behavior)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.8, 3.9_

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the offline suites touched so far: `test/backend-test/portal_builds/` (finite/non-watch) plus `bash -n setup-build-server.sh`; ensure all tests pass, ask the user if questions arise

- [x] 5. Fix-checking tests (bug-condition properties and unit tests)

  - [x] 5.1 Write the property test for target-aware OS release on the ephemeral plan
    - **Feature: jp7-ephemeral-runner-provisioning, Property 1: Target-aware OS release on the ephemeral plan**
    - In `test/backend-test/portal_builds/test_jp7_ephemeral_provisioning_properties.py`: for any supported build target planned as an ephemeral job (generated config_snapshots, sizing, spot flags), `plan_runner` yields a plan whose `(os_release, arch)` matches a frozen oracle deliberately re-spelled in the test — `JP7 -> ('24.04', 'arm64')`, every other target -> `('22.04', its required_arch)`
    - For a JP7 ephemeral job, `resolve_ami(plan.arch, plan.os_release)` resolves through the noble arm64 conventions: the recording SSM stub sees a parameter path containing `24.04` and the `ebs-gp3` segment, or on SSM failure a DescribeImages call carrying the Canonical owner id and the `hvm-ssd-gp3` noble name filter; assert no 22.04 parameter is read and no jammy env override is honored even when `BUILD_ARM64_AMI_ID` is set
    - Hypothesis, `@settings(max_examples=100, deadline=None)` minimum; run finite/non-watch with the property-test warning
    - **Validates: Requirements 2.1, 2.2**

  - [x] 5.2 Write the property test for fail-closed AMI resolution on unmapped pairings
    - **Feature: jp7-ephemeral-runner-provisioning, Property 2: Fail-closed AMI resolution for unmapped pairings**
    - In the same properties file: for any generated `(os_release, arch)` pairing outside `{('22.04','arm64'), ('22.04','x86_64'), ('24.04','arm64')}` (including `('24.04','x86_64')`, arbitrary release strings, and arbitrary arch strings), `resolve_ami` raises an error whose message names BOTH the release and the architecture, the recording stubs show ZERO AWS calls (no SSM GetParameter, no DescribeImages, no RunInstances), and the raised type is caught by `provision_ephemeral`'s `(ClientError, ValueError)` handler
    - Assert no fallback AMI of a different OS release is ever returned
    - Hypothesis, min 100 examples; run finite/non-watch with the property-test warning
    - **Validates: Requirements 2.3**

  - [x] 5.3 Write the property test for the JP7 dedicated capability gate
    - **Feature: jp7-ephemeral-runner-provisioning, Property 5: JP7 dedicated capability gate accepts exactly the capable hosts**
    - New `test/backend-test/portal_builds/test_jp7_dedicated_capability_properties.py`: for any JP7 + dedicated build request and any selected server record (generated `lifecycle_state`, `arch`, and `ubuntu_version` including an ABSENT field and arbitrary strings), `validate_build_request` accepts if and only if the server exists, `lifecycle_state = 'running'`, `arch = 'arm64'`, and recorded `ubuntu_version` is exactly `'24.04'` (absent treated as `'22.04'`, i.e. not capable)
    - When the release is the failing condition, some error carries rule `RULE_SERVER_OS_RELEASE_MISMATCH` with a message naming both Ubuntu 24.04 arm64 and the server's actual release; when other conditions also fail, their existing rules are still reported (the new gate masks no existing rejection)
    - Assert every rejection happens at validation time: the persistence recorder seam is empty, so no Build_Job record exists for a rejected request
    - Hypothesis, min 100 examples; run finite/non-watch with the property-test warning
    - **Validates: Requirements 2.7**

  - [x] 5.4 Write the property test for the single shared noble-delta implementation
    - **Feature: jp7-ephemeral-runner-provisioning, Property 6: Noble deltas live once in the shared bootstrap script and both seams reach them**
    - In `test/backend-test/portal_builds/test_jp7_ephemeral_provisioning_properties.py` (or a sibling script-level file): parse the fixed `setup-build-server.sh` and assert exactly one implementation of the deltas keyed on the detected release — under the detected-24.04 branch, `/usr/local/bin/docker-compose` is written with `exec docker compose "$@"` and made executable, the snap `docker-compose` install is unreachable on that branch, and the awscli, `botocore[crt]`, and GDK CLI installs carry `--break-system-packages` via the release-keyed flag
    - For any rendered launch user-data (`build_fleet.render_user_data`, generated repository/dir/ref, ANY `ubuntu_version` including 24.04) and any generated ephemeral bootstrap (`runner_bootstrap_user_data`, ANY target), the emitted body executes that same script and contains NO copy of any delta text (no shim body, no `--break-system-packages`)
    - Hypothesis, min 100 examples; run finite/non-watch with the property-test warning
    - **Validates: Requirements 2.6, 2.8**

  - [x] 5.5 Write unit tests for `required_os_release_for_target`
    - The five supported targets map to the frozen release table (`JP7 -> '24.04'`, the other four -> `'22.04'`); unsupported target names raise `ValueError`; existing `target_definition` consumers still read their named keys
    - _Requirements: 2.1, 2.2_

  - [x] 5.6 Write unit tests for the `resolve_ami` noble branch and override scoping
    - Noble SSM-parameter success returns the parameter's AMI id; DescribeImages fallback on SSM failure selects the newest image by CreationDate; `BUILD_ARM64_NOBLE_AMI_ID` takes precedence over both; the noble cache key returns the cached id on a second call without a second AWS call; a no-image resolution raises a diagnostic naming release and architecture
    - Jammy env overrides do NOT apply to noble resolution and the noble override does NOT apply to jammy resolution; the unmapped-pairing message names both release and arch
    - `provision_ephemeral` converts the unmapped-pairing `ValueError` into a failed job with `ERROR_PROVISIONING_FAILED` and a cause message carrying the pairing (stubbed persistence), terminating partial compute and auditing, never crashing the dispatcher tick
    - _Requirements: 2.1, 2.3, 3.2_

  - [x] 5.7 Write unit tests for the capability gate's example cases and rule composition
    - JP7 + dedicated rejected against `ubuntu_version` `'22.04'`, `'20.04'`, and ABSENT (pre-ec1dc38 record); accepted against `'24.04'`; the diagnostic names both the required capability and the actual release
    - The gate composes with existing rules: not-found, not-running, and arch-mismatch rejections still fire and are not masked when the release also mismatches; JP5/JP6 dedicated requests are accepted against a `'22.04'`, `'24.04'`, and field-less server alike
    - _Requirements: 2.7, 3.5, 3.9_

  - [x] 5.8 Write unit tests for `setup-build-server.sh` structure and syntax
    - Release detection present with the `/etc/os-release` read and `lsb_release -rs` fallback; the shim block (`exec docker compose "$@"`, `chmod +x`) appears under the 24.04 branch only; `--break-system-packages` reaches exactly the three PEP 668-affected installs via the release-keyed flag and no others; the script's `run_cmd`/error-summary conventions are kept for the new statements
    - `bash -n setup-build-server.sh` exits zero
    - _Requirements: 2.4, 2.6, 2.8, 3.8_

- [x] 6. Preservation property tests (fixed code)

  - [x] 6.1 Write the property test for non-JP7 planning and AMI resolution preservation
    - **Feature: jp7-ephemeral-runner-provisioning, Property 3: Non-JP7 planning and AMI resolution unchanged**
    - Extend `test/backend-test/portal_builds/test_jp7_ephemeral_preservation.py` against the task 2 frozen oracles: for any non-JP7 ephemeral Build_Job (generated config_snapshots, sizing, spot flags) the fixed `plan_runner` yields the same arch, instance type, volume size, spot flag, and status, and `os_release` equals the `'22.04'` default; and for any env-override configuration the fixed `resolve_ami` yields the same AMI id through the same order (override → per-container cache → the same 22.04 parameter path) for both architectures
    - Hypothesis, min 100 examples; run finite/non-watch with the property-test warning
    - **Validates: Requirements 3.1, 3.2, 3.4**

  - [x] 6.2 Write the property test for ephemeral bootstrap byte-identity
    - **Feature: jp7-ephemeral-runner-provisioning, Property 4: Ephemeral bootstrap text byte-identical, deltas via the shared seam**
    - For any ephemeral Build_Job of ANY supported target (generated source refs, repo dirs, regions), the fixed `runner_bootstrap_user_data` emits text byte-identical to the task 2 oracle, and the emitted text still ends with the root-written Bootstrap_Marker statement; the noble runner reaches the deltas only because the emitted build-user body executes `setup-build-server.sh`
    - Hypothesis, min 100 examples; run finite/non-watch with the property-test warning
    - **Validates: Requirements 2.4, 3.3**

  - [x] 6.3 Write the property test for 22.04 bootstrap byte-identity in both seams
    - **Feature: jp7-ephemeral-runner-provisioning, Property 7: 22.04 bootstrap byte-identical in both seams**
    - For any fleet launch with `ubuntu_version` 22.04 or absent (generated repository/dir/ref), the fixed `render_user_data`/`USER_DATA_TEMPLATE` renders text byte-identical to the task 2 oracle; and the fixed `setup-build-server.sh` on a detected 22.04 host executes the same effective command sequence as the frozen jammy oracle — the release-keyed flag expanding to nothing so the three install command lines are today's exactly, and the snap `docker-compose` branch reached exactly as today
    - Hypothesis, min 100 examples; run finite/non-watch with the property-test warning
    - **Validates: Requirements 3.3, 3.8**

  - [x] 6.4 Write the property test for `ubuntu_version`-invariant non-JP7 validation
    - **Feature: jp7-ephemeral-runner-provisioning, Property 8: Dedicated validation outcomes invariant to ubuntu_version for every non-JP7 request**
    - In `test/backend-test/portal_builds/test_jp7_dedicated_capability_properties.py`: for any build request whose targets do NOT include JP7 (JP5, JP6, AMD64, AMD64_NVIDIA, either mode) and any two fleets differing ONLY in the selected server's recorded `ubuntu_version` (`'22.04'`, `'24.04'`, an arbitrary other string, absent), the fixed `validate_build_request` yields the same accept/reject outcome and the same error rules for both, and that outcome equals the task 2 frozen oracle
    - Hypothesis, min 100 examples; run finite/non-watch with the property-test warning
    - **Validates: Requirements 3.5, 3.9**

- [x] 7. Checkpoint - Ensure all tests pass
  - Run the offline `test/backend-test/portal_builds/` suite plus the script-level tests and `bash -n setup-build-server.sh`, finite/non-watch, with the property-test warning; ensure all tests pass, ask the user if questions arise

- [x] 8. Integration tests

  - [x] 8.1 Write the JP7 ephemeral dispatcher-tick integration test
    - Following `test/backend-test/portal_builds/test_dispatcher_tick_integration.py` conventions with stubbed AWS seams: a queued JP7 ephemeral job is provisioned with the noble AMI id returned by the stub, the RunInstances call carries that ImageId, the runner record carries `os_release='24.04'`, and the readiness/Bootstrap_Marker/agent-command flow proceeds unchanged
    - _Requirements: 2.1, 2.2, 2.4_

  - [x] 8.2 Write the mixed-batch tick integration test
    - A JP5 and a JP7 ephemeral job in one tick: each is provisioned from its own release's AMI (jammy parameter for JP5, noble parameter for JP7), the JP5 job's plan fields, resolved AMI, and bootstrap text are byte-preserved against the task 2 oracle, and neither job's cache entry pollutes the other
    - _Requirements: 2.1, 3.1, 3.3_

  - [x] 8.3 Write the unmapped-pairing tick integration test
    - A synthetic plan with an unmapped release/arch pairing in a multi-job batch fails EXACTLY that one job with `ERROR_PROVISIONING_FAILED` and a cause naming the pairing; the tick does not raise; the other jobs in the batch are provisioned normally; no instance is launched for the failed job
    - _Requirements: 2.3_

  - [x] 8.4 Write the request-to-rejection dedicated flow integration test
    - A JP7 + dedicated submission selecting a jammy server (and again a pre-ec1dc38 server with no `ubuntu_version`) is rejected at the API validation boundary with the capability diagnostic and NO job record is created
    - The same submission against a running 24.04 arm64 server is accepted and dispatches through the unchanged dedicated machinery: exact-selected-server allocation, single running slot, queueing, and pre-dispatch pgrep verification all behave as today
    - _Requirements: 2.7, 3.6_

- [x] 9. Final checkpoint - Ensure all tests pass
  - Run the complete offline backend suite `test/backend-test/` unmodified (finite/non-watch, with the property-test warning) and confirm every existing test still passes with no change to any existing test's expected values for non-JP7 behavior
  - _Requirements: 3.7_

- [ ] 10. Live on-host verification (optional, long-running)

  - [ ]* 10.1 Verify the fix on a dedicated Ubuntu 24.04 build machine FIRST
    - Ordered first per the user's verification preference: a dedicated host retains docker layer caches across attempts, so fix iteration is substantially faster than the ephemeral path
    - Launch a 24.04 arm64 Dedicated_Build_Server from the portal fleet page, confirm the bootstrap completes with NO manual operator steps (the `docker-compose` shim present and executable, awscli / `botocore[crt]` / GDK CLI installed under PEP 668), then submit and run a JP7 dedicated build end to end (hours-long; one build at a time per builds.md, logging to `.gdk_build_jp7.log`)
    - Also confirm the negative case live: a JP7 dedicated submission against a 22.04 arm64 server is rejected with the capability diagnostic and creates no job
    - _Requirements: 2.6, 2.7, 2.8_

  - [ ]* 10.2 Validate the ephemeral path end-to-end on a fresh noble runner
    - After 10.1 passes: dispatch a JP7 ephemeral build and confirm the full flow on cold caches — noble arm64 AMI provisioned, runner record `os_release='24.04'`, SSM online and Bootstrap_Marker readiness gate satisfied, `setup-build-server.sh` applying the noble deltas on the fresh runner, and the build proceeding past host setup through publish (hours-long, fresh runner per attempt)
    - Confirm a JP5 ephemeral build dispatched in the same window still provisions from the jammy AMI and succeeds unchanged
    - _Requirements: 2.1, 2.2, 2.4, 3.1, 3.3_

## Notes

- Tasks 1 and 2 run on UNFIXED code only. Task 1 MUST FAIL (its failures are the counterexamples proving the bug); task 2 MUST PASS (it freezes the preservation oracles). Neither may be rewritten or weakened after implementation — 3.8 and 3.9 re-run those same files
- Property numbering follows `design.md` Correctness Properties 1–8: Properties 1, 2, 5, 6 are bug-condition (fix-checking) properties; Properties 3, 4, 7, 8 are preservation properties. Task 1 carries Property 1 and task 2 carries Property 3 for hover status
- Property tests use Hypothesis with `@settings(max_examples=100, deadline=None)` minimum, live in `test/backend-test/portal_builds/`, and are tagged `**Feature: jp7-ephemeral-runner-provisioning, Property N: <title>**`
- Preservation oracles are frozen values observed on the unfixed code and deliberately re-spelled in the tests (the suite's `FROZEN_MATRIX` precedent) rather than captured by importing the pre-fix code
- The noble deltas have exactly ONE implementation, in `setup-build-server.sh` (3.7). No delta text is added to `build_fleet.USER_DATA_BODY` or `build_dispatcher.runner_bootstrap_user_data`; both already execute the script, which is what keeps every jammy rendering byte-identical (3.3, 3.8)
- `build_fleet.py` (`USER_DATA_BODY`, `render_user_data`, `resolve_ubuntu_ami`, `run_fleet_instance`), `plan_dedicated_dispatch` and the dedicated dispatch machinery, the job state machine, readiness gate, preflight, and reconciliation are all deliberately unchanged
- All test commands are finite/non-watch; property-test runs carry the warning `This test run contains property-based tests and may generate/shrink counterexamples.`
- Tasks marked with `*` (10.1, 10.2) are optional hours-long live build activities and are not part of the offline merge gates; the required gate is task 9's full `test/backend-test/` run
