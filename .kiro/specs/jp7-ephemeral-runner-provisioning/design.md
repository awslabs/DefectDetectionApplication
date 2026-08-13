# JP7 Ephemeral Runner Provisioning Bugfix Design

## Overview

JP7 portal builds fail in **both execution modes**. Ephemeral: JP7 jobs are provisioned onto Ubuntu 22.04 (jammy) arm64 runners and fail mid-build — `build_dispatcher.resolve_ami(arch)` is keyed only by CPU architecture, and `build_planner.RunnerPlan` carries no OS release, so the dispatcher cannot select the Ubuntu 24.04 (noble) arm64 host that JP7 requires (jetpack7-support design §10, README §4.2). Dedicated: the fleet launch path (`build_fleet.py`, since commit ec1dc38) can resolve a noble AMI, but it renders the same `USER_DATA_BODY` bootstrap for every release — a plain `setup-build-server.sh` run — and that script is actively noble-incompatible (snap `docker-compose` instead of the documented shim; `pip3` installs without `--break-system-packages` fail under noble's PEP 668 externally-managed Python), so a portal-launched 24.04 server is NOT JP7-build-capable without manual steps (README §4.2's own note). Independently, JP7 dedicated dispatch has no capability gate: `build_domain.validate_build_request` checks only existence/running/arch, so a jammy arm64 server accepts a JP7 dedicated job and fails mid-build exactly like the ephemeral path (contrary to jetpack7-support Req 6.4/6.5 fail-closed semantics).

**Fix approach — ephemeral (primary path, bugfix.md 2.1–2.5)**: make OS-release selection target-aware on the ephemeral path.

1. `build_domain.BUILD_TARGETS` gains a `required_os_release` per target (derived alongside `required_arch`; `'22.04'` for all existing targets, `'24.04'` for JP7), with a `required_os_release_for_target(target)` accessor.
2. `build_planner.RunnerPlan` gains an `os_release` field (appended, with default `'22.04'` so every existing construction and field-wise comparison is unaffected); `plan_runner` derives it from the job's build target.
3. `build_dispatcher.resolve_ami` becomes `resolve_ami(arch, os_release='22.04')`. The 22.04 branch is byte-for-byte today's logic (env override → per-container cache → canonical SSM parameter). The 24.04 branch mirrors the conventions already established in `build_fleet.py`: the canonical noble arm64 SSM parameter (with its `ebs-gp3` path segment), a Canonical-owner `DescribeImages` fallback with the `hvm-ssd-gp3` noble name filter, and fail-closed behavior on unmapped release/arch pairings — raised BEFORE any AWS call.
4. The noble runner receives the noble host deltas through the SHARED bootstrap seam (point 5 below): the ephemeral bootstrap's build-user body already executes `setup-build-server.sh`, so the runner user-data text itself does not change.

**Fix approach — dedicated (bugfix.md 2.6–2.8)**: put the noble deltas in the one bootstrap seam both modes share, and gate JP7 dedicated dispatch on the recorded host release.

5. **Shared noble deltas (2.6, 2.8)**: `setup-build-server.sh` itself gains the noble deltas, keyed on the DETECTED OS release of the host it runs on: the `docker-compose` shim delegating to the `docker compose` plugin (never the snap install on noble), and `--break-system-packages` on the three PEP 668-affected pip installs (awscli, `botocore[crt]`, GDK CLI). Because both user-data paths (`build_fleet.USER_DATA_BODY` for fleet launches and `build_dispatcher.runner_bootstrap_user_data`'s build-user body for ephemeral runners) already execute this script, both get the deltas for free — ONE implementation, no divergent copies (2.8), zero user-data text changes, and a portal-launched 24.04 server comes up JP7-build-capable without manual operator steps (2.6). On a 22.04 host the release conditioning leaves the executed command sequence identical to today (3.8).
6. **Capability gate (2.7)**: `build_domain.validate_build_request` rejects JP7 + dedicated when the selected server's recorded `ubuntu_version` is not `'24.04'` (an absent field means a pre-ec1dc38 server, i.e. a 22.04 host), with a diagnostic naming the missing capability (Ubuntu 24.04 arm64) and the server's actual release — at validation time, before any job is created. The gate keys on `required_os_release_for_target`, so JP5/JP6 dedicated requests (which require `'22.04'` and run fine anywhere) are accepted regardless of the server's `ubuntu_version` (3.9).

**Feasibility decision (bugfix.md 2.5)**: noble ephemeral provisioning is judged **feasible**; the fallback (reject JP7+ephemeral at validation) is **not taken**. Evidence: (a) the canonical noble arm64 SSM parameter and DescribeImages fallback are already exercised by production code (`build_fleet.resolve_ubuntu_ami`, used by the dedicated-fleet launch path); (b) canonical noble AMIs ship the SSM agent preinstalled, so the existing SSM-online/Bootstrap_Marker readiness gate applies unchanged; (c) the noble host deltas are small, enumerated, and already validated end-to-end by the documented manual JP7 build-server flow (README §4.2): a two-line `docker-compose` shim and the PEP 668 pip flag; (d) the instance sizing/profile/tag machinery in `run_runner_instance` is release-agnostic. No blocking gap was identified. The dedicated-mode changes (2.6–2.8) stand regardless of this decision.

## Glossary

- **Bug_Condition (C)**: a build job with `build_target = 'JP7'` is dispatched, in either execution mode, onto a host lacking the noble JP7 prerequisites — ephemeral: the runner is provisioned from the wrong OS release (jammy); dedicated: the selected server is either a portal-launched 24.04 server missing the noble bootstrap deltas, or a wrong-OS (22.04) server that validation never screened out.
- **Property (P)**: the desired behavior for buggy inputs — the JP7 ephemeral runner plan carries OS release 24.04 and the AMI resolves through the noble arm64 conventions; a 24.04 fleet launch produces a JP7-build-capable server (noble deltas applied by the shared bootstrap); JP7 + dedicated on a non-24.04 server is rejected at validation with a capability diagnostic.
- **Preservation**: for every input where C does not hold (JP5/JP6/AMD64/AMD64_NVIDIA in both modes, 22.04 launches and bootstraps, request validation for every other combination), the fixed code produces identical outputs to the original code.
- **jammy / noble**: Ubuntu 22.04 LTS / Ubuntu 24.04 LTS. Jammy is today's only ephemeral runner OS and the fleet default; noble is the JP7 build host.
- **RunnerPlan**: `build_planner.RunnerPlan`, the pure provisioning plan (one per dispatched ephemeral job) the dispatcher executes.
- **resolve_ami**: `build_dispatcher.resolve_ami`, the ephemeral-path AMI resolution (env override → cache → canonical SSM parameter). Distinct from `build_fleet.resolve_ubuntu_ami`, the dedicated-fleet counterpart that already handles noble.
- **runner_bootstrap_user_data**: `build_dispatcher.runner_bootstrap_user_data`, the generated cloud-init user-data for ephemeral runners (root prologue + build-user body running `setup-build-server.sh`).
- **USER_DATA_BODY**: `build_fleet.USER_DATA_BODY`, the fleet-launch user-data bootstrap (clone + Source_Sync + `setup-build-server.sh` + Bootstrap_Marker), rendered identically for every release.
- **setup-build-server.sh**: the repository's build-environment bootstrap script, executed by BOTH user-data seams — the one shared implementation point for the noble deltas (2.8).
- **ubuntu_version**: the OS release recorded on a Dedicated_Build_Server record at launch (since commit ec1dc38). Servers launched before that change carry no field and are 22.04 hosts; the capability gate treats absence as `'22.04'`.
- **Capability gate**: the new `validate_build_request` rule rejecting JP7 + dedicated when the selected server's recorded release is not 24.04 (jetpack7-support Req 6.4/6.5 fail-closed semantics) — a jammy host can never run a JP7 build regardless of applied deltas, so the deltas alone cannot substitute for the gate.
- **docker-compose shim**: `/usr/local/bin/docker-compose` delegating to the `docker compose` plugin (README §4.2) — required because noble ships no legacy `docker-compose` binary while `build-custom.sh` invokes `docker-compose`.
- **PEP 668**: noble's python is "externally managed"; `pip3 install --user` / `sudo pip3 install` (both used by `setup-build-server.sh`) fail without `--break-system-packages`.
- **Bootstrap_Marker**: the completion marker the readiness gate probes before the agent command is sent (unchanged by this fix).

## Bug Details

### Bug Condition

**Ephemeral**: when a JP7 build job is dispatched in the ephemeral execution mode, `plan_runner` produces a RunnerPlan carrying only the CPU architecture (arm64), `resolve_ami('arm64')` selects the jammy arm64 AMI, and the runner comes up as a jammy host missing the noble toolchain — the build fails mid-run.

**Dedicated**: when a JP7 build job is dispatched in the dedicated execution mode, no layer produces a capable host. A portal launch with `ubuntu_version=24.04` resolves a noble AMI but renders the release-blind `USER_DATA_BODY`, and `setup-build-server.sh` on the noble host installs `docker-compose` via snap instead of the shim and fails its PEP 668-affected pip installs — the server registers in the fleet but is not JP7-build-capable. And `validate_build_request` accepts a JP7 dedicated request against ANY running arm64 server (it never reads `ubuntu_version`), so a jammy server takes the job and fails mid-build exactly like the ephemeral path.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type Build_Job (dispatched)
  OUTPUT: boolean

  RETURN input.build_target = 'JP7'
         AND (
              -- ephemeral: runner provisioned from the wrong OS release
              input.execution_mode = 'ephemeral'
           OR -- dedicated: wrong-OS server never screened out at validation
              (input.execution_mode = 'dedicated'
               AND recordedRelease(selectedServer(input)) != '24.04')
                   -- recordedRelease: ubuntu_version, absent => '22.04'
           OR -- dedicated: portal-launched 24.04 server whose bootstrap
              -- never applied the noble deltas (release-blind USER_DATA_BODY
              -- + noble-incompatible setup-build-server.sh)
              (input.execution_mode = 'dedicated'
               AND recordedRelease(selectedServer(input)) = '24.04'
               AND NOT nobleDeltasApplied(selectedServer(input)))
         )
END FUNCTION
```

### Examples

- A JP7 ephemeral job is dispatched: `plan_runner` yields `RunnerPlan(arch='arm64', ...)` with no OS release; `resolve_ami('arm64')` reads `/aws/service/canonical/ubuntu/server/22.04/stable/current/arm64/hvm/ebs-gp2/ami-id` (or `BUILD_ARM64_AMI_ID`, a jammy pin). Expected: a noble arm64 AMI. Actual: a jammy arm64 AMI.
- The JP7 build runs on the jammy runner: `build-custom.sh` invokes `docker-compose` and the JP7 docker build requires the noble host environment; the build fails mid-run. Expected: build proceeds past host setup on a noble host.
- A Dedicated_Build_Server is launched from the fleet page with `ubuntu_version=24.04`: the rendered user-data is byte-identical to a 22.04 launch, and on the noble host `setup-build-server.sh`'s `sudo pip3 install awscli`, `sudo pip3 install 'botocore[crt]'`, and `pip3 install --user git+...gdk-cli` are rejected by PEP 668 while `docker-compose` is snap-installed instead of shimmed. Expected: the launched server is JP7-build-capable without manual steps. Actual: README §4.2's manual "steps 2–3" remain required.
- A JP7 dedicated request selects a running arm64 server with `ubuntu_version='22.04'` (or no field at all): `validate_build_request` accepts it (existence + running + arch all pass) and the build fails mid-run on the jammy host. Expected: rejection at validation naming the missing Ubuntu 24.04 arm64 capability and the server's actual release, before any job is created.
- Edge case (post-fix surface): a hypothetical target requiring 24.04 on x86_64 has no AMI mapping — ephemeral resolution must fail closed naming the pairing, never fall back to a jammy AMI.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- JP5/JP6/AMD64/AMD64_NVIDIA ephemeral AMI selection: identical resolution order (explicit `BUILD_ARM64_AMI_ID`/`BUILD_X86_64_AMI_ID` env override first, then the per-container cache, then the per-architecture canonical 22.04 SSM parameters), producing the identical AMI for identical inputs (3.1, 3.2).
- The generated ephemeral bootstrap user-data: byte-identical text for ALL targets — the noble deltas travel in `setup-build-server.sh`, not in user-data (3.3, strengthened from the jammy-only guarantee of the earlier draft).
- `plan_runner` outputs for non-JP7 targets: same architecture, instance type, volume size, spot flag, and status for the same job inputs (3.4).
- Build request validation: every currently accepted/rejected target–mode combination other than JP7+dedicated keeps its outcome; JP7+ephemeral remains accepted (primary path); only JP7+dedicated-on-a-non-24.04-server flips from accepted to rejected — which is exactly the fix of 2.7 (3.5).
- JP7 dedicated dispatch to a CAPABLE (noble arm64) server: the existing dedicated machinery is untouched — exact-selected-server allocation, single running slot, queueing, pre-dispatch pgrep verification; the fix adds only the capability gate in front of it (3.6).
- The 22.04 fleet launch: `USER_DATA_BODY`/`render_user_data` output byte-identical to today for every release (the deltas live in the script), and `setup-build-server.sh` on a 22.04 host executes the same effective command sequence and produces the same build environment as today (3.8).
- JP5/JP6 dedicated requests: accepted and dispatched exactly as today against any running arm64 server, regardless of the server's `ubuntu_version` — the capability gate constrains JP7 only (3.9).
- All existing offline suites under `test/backend-test/` (including `test/backend-test/portal_builds/`) pass without modification (3.7).

**Scope:**
All inputs that do NOT involve a JP7 build are completely unaffected:
- Every non-JP7 ephemeral job (planning, AMI resolution, bootstrap content, runner record shape for existing fields).
- Every non-JP7 dedicated job (validation outcome, allocation, dispatch), on any server release.
- Every 22.04 fleet launch and every `setup-build-server.sh` run on a 22.04 host.
- The job state machine, watchdogs, readiness gating, preflight, and reconciliation.

**Note:** the expected correct behavior for the buggy inputs is defined in the Correctness Properties section (Properties 1, 2, 5, 6).

## Hypothesized Root Cause

The bug description and code reading establish the causes directly (missing-dimension defects, not subtle logic errors):

1. **Missing OS-release dimension in the plan**: `RunnerPlan` has fields for arch/sizing/spot/status only; `plan_runner` derives arch via `required_arch_for_target` and nothing else about the host. The domain table (`BUILD_TARGETS`) itself has no OS-release attribute to derive from.

2. **Arch-only AMI resolution**: `resolve_ami(arch)` picks between exactly two SSM parameters (both 22.04) and two env overrides; there is no release parameter and no noble mapping. The dedicated-fleet counterpart (`build_fleet.resolve_ubuntu_ami(arch, ubuntu_version)`) already solved this — the ephemeral path was simply never given the counterpart.

3. **Release-blind, noble-incompatible shared bootstrap**: `build_fleet.USER_DATA_BODY` renders the same plain `setup-build-server.sh` run for every release, and the script itself cannot succeed on noble: it snap-installs `docker-compose` (noble needs the shim delegating to the `docker compose` plugin — `build-custom.sh` invokes `docker-compose` by name), and its `sudo pip3 install awscli`, `sudo pip3 install 'botocore[crt]'`, and `pip3 install --user git+...gdk-cli` lack `--break-system-packages`, failing under noble's PEP 668 externally-managed Python. The ephemeral bootstrap body runs the SAME script, so both seams share the same defect — and can share the same fix (2.8).

4. **Missing dedicated capability gate**: `validate_build_request` checks only server existence, running lifecycle state, and CPU architecture — never the recorded `ubuntu_version` or any JP7 capability — and `plan_dedicated_dispatch` targets exactly the selected server with no capability screen, so no missing-capability diagnostic is produced before build work starts (contrary to jetpack7-support Req 6.4/6.5). The server record has carried `ubuntu_version` since commit ec1dc38 (absent = pre-change 22.04 host), so the gate has authoritative data to key on.

5. **Exception-contract detail (confirmed by reading `provision_ephemeral`)**: the provisioning loop catches `(ClientError, ValueError)` and routes to `fail_provisioning`. `build_fleet.resolve_ubuntu_ami` raises `RuntimeError` on unmapped pairings; a naive copy of that convention into the dispatcher would escape the catch and crash the tick instead of failing the one job. The fix therefore raises `ValueError` for unmapped pairings on the ephemeral path (2.3: "fail the build job with a provisioning error naming the unmapped pairing").

## Correctness Properties

Property 1: Bug Condition - Target-aware OS release on the ephemeral plan

_For any_ supported build target planned as an ephemeral job (isBugCondition inputs and their non-buggy siblings alike), the fixed `plan_runner` SHALL produce a RunnerPlan whose (os_release, arch) pairing matches a frozen oracle re-spelled in the test — `JP7 -> ('24.04', 'arm64')`, and every other target -> `('22.04', its required_arch)` — and for a JP7 ephemeral job the fixed `resolve_ami(plan.arch, plan.os_release)` SHALL resolve through the noble arm64 conventions (the canonical noble arm64 SSM parameter with the `ebs-gp3` path segment, falling back to Canonical-owner DescribeImages with the `hvm-ssd-gp3` noble name filter), never through any 22.04 parameter or jammy env override.

**Validates: Requirements 2.1, 2.2**

Property 2: Bug Condition - Fail-closed AMI resolution for unmapped pairings

_For any_ OS-release/architecture pairing outside the mapped set {('22.04','arm64'), ('22.04','x86_64'), ('24.04','arm64')}, the fixed `resolve_ami` SHALL raise an error whose message names both the OS release and the architecture of the unmapped pairing, SHALL make no AWS call (no SSM GetParameter, no EC2 DescribeImages/RunInstances), and SHALL raise an exception type the `provision_ephemeral` catch converts into a failed job with a provisioning error (never a fallback AMI of a different OS release).

**Validates: Requirements 2.3**

Property 3: Preservation - Non-JP7 planning and AMI resolution unchanged

_For any_ non-JP7 ephemeral Build_Job (arbitrary generated config_snapshots, sizing, spot flags) and _for any_ env-override configuration of `BUILD_ARM64_AMI_ID`/`BUILD_X86_64_AMI_ID`, the fixed `plan_runner` SHALL produce the same architecture, instance type, volume size, spot flag, and status as the original function, and the fixed `resolve_ami` SHALL produce the same AMI id through the same resolution order (env override, then per-container cache, then the same 22.04 SSM parameter) as the original function.

**Validates: Requirements 3.1, 3.2, 3.4**

Property 4: Preservation - Ephemeral bootstrap text byte-identical, deltas via the shared seam

_For any_ ephemeral Build_Job of ANY supported target (arbitrary source refs, repo dirs, regions), the fixed `runner_bootstrap_user_data` SHALL emit text byte-identical to the original function's output for the same inputs — the noble host deltas reach a noble runner NOT through user-data text but because the emitted build-user body executes `setup-build-server.sh`, the shared seam whose release-keyed deltas Property 6 pins — and the emitted text SHALL still end with the root-written Bootstrap_Marker statement.

**Validates: Requirements 2.4, 3.3**

Property 5: Bug Condition - JP7 dedicated capability gate accepts exactly the capable hosts

_For any_ build request combining build_target JP7 with execution_mode dedicated and _for any_ selected server record (generated `lifecycle_state`, `arch`, and `ubuntu_version` including an absent field), the fixed `validate_build_request` SHALL accept the request **if and only if** the selected server exists, its `lifecycle_state` is `running`, its `arch` is `arm64`, and its recorded `ubuntu_version` is exactly `'24.04'` (an absent field treated as `'22.04'`, i.e. NOT capable). When the release is the failing condition, the rejection SHALL carry rule `RULE_SERVER_OS_RELEASE_MISMATCH` with a message naming both the missing JP7 host capability (Ubuntu 24.04 arm64) and the server's actual release; when other conditions also fail, their existing rules SHALL still be reported (the new gate masks no existing rejection). Every rejection SHALL be produced at validation time, so no Build_Job record is created for a rejected request.

**Validates: Requirements 2.7** (closes defect clauses 1.7, 1.8)

Property 6: Bug Condition - Noble deltas live once, in the shared bootstrap script, and both seams reach them

The fixed `setup-build-server.sh` SHALL contain exactly one implementation of the noble deltas, keyed on the detected OS release of the host: under the detected-24.04 branch it SHALL provide `/usr/local/bin/docker-compose` as a shim delegating to the `docker compose` plugin (and SHALL NOT reach the snap `docker-compose` install on that branch), and its awscli, `botocore[crt]`, and GDK CLI pip installs SHALL carry `--break-system-packages`; and _for any_ rendered launch user-data (`build_fleet.render_user_data`, arbitrary repository/dir/ref, ANY `ubuntu_version`) and _for any_ generated ephemeral bootstrap (`runner_bootstrap_user_data`, ANY target), the emitted body SHALL execute that same script and SHALL contain no copy of any delta text — so a 24.04 host reaches the deltas through the single implementation from either seam, which is what makes a portal-launched 24.04 server JP7-build-capable with no manual operator step.

**Validates: Requirements 2.6, 2.8** (closes defect clauses 1.5, 1.6)

Property 7: Preservation - 22.04 bootstrap byte-identical in both seams

_For any_ fleet launch with `ubuntu_version` 22.04 or absent (arbitrary repository/dir/ref), the fixed `render_user_data`/`USER_DATA_TEMPLATE` SHALL render text byte-identical to the original function's output for the same inputs, and the fixed `setup-build-server.sh` SHALL, on a detected 22.04 host, execute the same effective command sequence as the original script — the release-conditioned pip flag expanding to nothing so the three install command lines are today's exactly, and the snap `docker-compose` branch reached exactly as today.

**Validates: Requirements 3.3, 3.8**

Property 8: Preservation - Dedicated validation outcomes invariant to ubuntu_version for every non-JP7 request

_For any_ build request whose targets do NOT include JP7 (JP5, JP6, AMD64, AMD64_NVIDIA, in either execution mode) and _for any_ two fleets differing ONLY in the selected server's recorded `ubuntu_version` (`'22.04'`, `'24.04'`, an arbitrary other string, or the field absent), the fixed `validate_build_request` SHALL produce the same accept/reject outcome and the same error rules for both — and that outcome SHALL equal the original function's outcome. Equivalently: `ubuntu_version` is not an input to any validation decision except the JP7 gate of Property 5.

**Validates: Requirements 3.5, 3.9**

## Fix Implementation

### Changes Required

Assuming the root cause analysis above:

**File**: `edge-cv-portal/backend/functions/build_domain.py`

1. **OS-release attribute per target**: each `BUILD_TARGETS` entry gains `'required_os_release'` (`OS_RELEASE_JAMMY = '22.04'` for JP5/JP6/AMD64/AMD64_NVIDIA, `OS_RELEASE_NOBLE = '24.04'` for JP7), plus a `required_os_release_for_target(target)` accessor delegating to `target_definition` (so unsupported targets keep raising `ValueError`). Existing readers (`create_build_jobs`, `retry_clone`, dispatch preflight) select named keys and are unaffected. (If the suite run surfaces an exact-shape assertion on `target_definition`'s dict, the fallback is a separate module-level mapping with the same accessor — the accessor is the only API the rest of the fix uses.)

2. **JP7 dedicated capability gate in `validate_build_request` (2.7)**: inside the existing dedicated-server validation block (after the arch-mismatch check, same found-and-running server record in hand), for each supported selected target whose `required_os_release_for_target(target)` is `'24.04'` (today: JP7 only), compare against the server's recorded release — `server.get('ubuntu_version') or '22.04'`, so pre-ec1dc38 records with no field are treated as the 22.04 hosts they are. On mismatch, append a rejection with a new rule id `RULE_SERVER_OS_RELEASE_MISMATCH` and a message naming the missing capability and the actual release, e.g. *"Dedicated_Build_Server '<id>' runs Ubuntu <actual>, but Build_Target JP7 requires an Ubuntu 24.04 arm64 build host. Select a 24.04 arm64 server (or use the ephemeral execution mode)."* Rejection at validation means no job record is ever created (jetpack7-support Req 6.4/6.5 fail-closed semantics). Targets requiring `'22.04'` impose NO release constraint — JP5/JP6 stay accepted on any server release, including noble (3.9). `plan_dedicated_dispatch` needs no change: the gate in front of job creation is sufficient (3.6), and retry-clone of a pre-fix JP7 job re-enters through the same machinery it always did.

**File**: `edge-cv-portal/backend/functions/build_planner.py`

3. **`RunnerPlan.os_release`**: appended as the LAST field with default `'22.04'`, so existing positional constructions, field access, and field-wise comparisons are unchanged. `plan_runner` sets it from `build_domain.required_os_release_for_target(job['build_target'])`. For every existing target the derived value equals the default.

**File**: `edge-cv-portal/backend/functions/build_dispatcher.py`

4. **Noble AMI constants (mirroring `build_fleet.py` conventions)**:
   - `ARM64_NOBLE_AMI_SSM_PARAMETER` (env-overridable, default `/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id` — note the `ebs-gp3` segment, verified against the canonical parameter tree by build_fleet).
   - `CANONICAL_OWNER_ID = '099720109477'` and the noble arm64 name filter `ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*` for the DescribeImages fallback.
   - `BUILD_ARM64_NOBLE_AMI_ID` — a NEW optional env override for the noble arm64 AMI. **Override interaction decision**: the existing `BUILD_ARM64_AMI_ID`/`BUILD_X86_64_AMI_ID` overrides are scoped to 22.04 only (they are jammy pins today; letting a jammy pin silently override a noble runner would reintroduce the bug through configuration). Noble pinning uses only the new variable. This is documented on the constants.

5. **`resolve_ami(arch, os_release='22.04')`**:
   - `os_release == '22.04'`: today's body unchanged — `BUILD_ARM64_AMI_ID`/`BUILD_X86_64_AMI_ID` override, then `_AMI_CACHE[arch]`, then the existing 22.04 SSM parameter (cache keyed exactly as today, preserving per-container cache semantics, 3.2).
   - `os_release == '24.04'` and `arch == 'arm64'`: `BUILD_ARM64_NOBLE_AMI_ID` override, then a noble cache entry (a distinct cache key, e.g. `'24.04/arm64'`, never colliding with the jammy keys), then the noble SSM parameter, then the Canonical-owner DescribeImages fallback (newest available image by CreationDate), caching the result. A resolution that finds no image raises with a diagnostic naming release and architecture.
   - Any other pairing: `raise ValueError(f"No Ubuntu {os_release} AMI mapping for architecture {arch} on the ephemeral runner path")` — BEFORE any AWS call. `ValueError` (not `build_fleet`'s `RuntimeError`) is deliberate: `provision_ephemeral` already catches `(ClientError, ValueError)` and routes to `fail_provisioning`, which fails the job with `ERROR_PROVISIONING_FAILED` and a cause message carrying the pairing (2.3), terminates partial compute, and audits — no dispatcher-tick crash.

6. **`run_runner_instance`**: `'ImageId': resolve_ami(plan.arch, plan.os_release)`. **The bootstrap user-data is NOT release-parameterized** (revised from the earlier ephemeral-only draft, which planned a noble prologue in `runner_bootstrap_user_data`): the noble deltas now live in `setup-build-server.sh` (change 8), which the build-user body already executes, so `runner_bootstrap_user_data` is unchanged and its output stays byte-identical for every target (3.3, Property 4). This is exactly the one-implementation sharing Req 2.8 mandates — a dispatcher-side prologue would have been the second divergent copy.

7. **`provision_ephemeral`**: record `'os_release': plan.os_release` on the runner record (additive diagnostic field alongside `arch`/`instance_type`). No control-flow change.

**File**: `setup-build-server.sh` (repository root — the SHARED bootstrap seam, 2.6/2.8)

8. **Release-keyed noble deltas, one implementation**:
   - **Detection**: near the top, detect the host release once — `OS_RELEASE=$(. /etc/os-release && echo "$VERSION_ID")`, falling back to `lsb_release -rs` (the mechanism the script's Python-3.11 branch already uses) — and derive `PIP_BREAK_FLAG='--break-system-packages'` when `OS_RELEASE` is `24.04`, empty otherwise.
   - **docker-compose**: the existing `if ! command -v docker-compose` block branches on the release: on 24.04, write the shim `/usr/local/bin/docker-compose` (`#!/bin/sh` + `exec docker compose "$@"`, per README §4.2) and `chmod +x` it — the snap `docker-compose` install is never attempted on noble; on 22.04 the snap branch runs verbatim as today.
   - **PEP 668 pip installs**: the three affected invocations — `sudo pip3 install awscli`, `sudo pip3 install --no-compile 'botocore[crt]'`, and `pip3 install --user git+...aws-greengrass-gdk-cli...` — gain `$PIP_BREAK_FLAG`. On 22.04 the flag expands to nothing, so the executed commands are identical to today (jammy's pip would reject the unknown option, which is precisely why the flag must be release-conditioned rather than unconditional); on noble they pass `--break-system-packages` (2.6). Existing `run_cmd`/error-summary conventions of the script are kept for every new statement.
   - **Sharing mechanism (2.8)**: this script is the one implementation. `build_fleet.USER_DATA_BODY` (fleet launches) and `build_dispatcher.runner_bootstrap_user_data`'s build-user body (ephemeral runners) both already execute it, so BOTH modes receive the deltas with zero user-data changes — no divergent copies, and a portal-launched 24.04 server comes up JP7-build-capable without the manual README §4.2 steps (2.6). Runners/servers receive the updated script through the existing Source_Sync onto the selected ref, the same mechanism that delivers the build agent itself.

### Single Source of Truth for the Noble Deltas (2.8) — decision and alternatives

**Decision**: the deltas live **inside `setup-build-server.sh`, keyed on the OS release the script detects on the host it is running on**. Neither user-data seam gains any delta text; both inherit the deltas because both already execute the script.

Alternatives considered:

| Option | Placement | Verdict |
|---|---|---|
| **A (chosen)** | Release-detecting branches inside `setup-build-server.sh` | Both seams inherit one implementation. Jammy renderings of BOTH user-data paths stay byte-identical (3.3, 3.8) with no argument, flag, or template change to prove identical. The script's jammy behavior is preserved by construction: the release conditioning expands to today's command text on a detected 22.04 host, and the script already reads the host release (`lsb_release -rs` in its Python 3.11 block), so no new mechanism is introduced. |
| B | A separate shared snippet module (e.g. a `noble_deltas.sh` sourced by the script, or a shared Python constant interpolated into both user-data bodies) | Rejected. The Python-constant form parameterizes user-data by release, which means the jammy rendering must be proven byte-identical against a template that now has a release-conditioned region — exactly the byte-identity risk 3.3/3.8 forbid taking for no benefit. The sourced-snippet form adds a second file that must reach the host, and it can only reach the host through the same Source_Sync that already delivers `setup-build-server.sh`, so it buys nothing over A while adding a delivery dependency and a second place a reviewer must look. |
| C | Deltas duplicated in `build_fleet.USER_DATA_BODY` and `build_dispatcher.runner_bootstrap_user_data` | Rejected outright: this is the two-divergent-copies outcome 2.8 explicitly prohibits, and it changes both jammy renderings. |

**Consequence for the "24.04 rendering" expectation**: under Option A the rendered 24.04 launch user-data does not itself *contain* delta text — it *reaches* the deltas by invoking the script whose noble branch carries them. The verification obligation is therefore split across two script-level checks, both offline: the rendered body invokes `setup-build-server.sh` (reachability, Property 6) and the script's noble branch contains the deltas while its jammy branch is today's text (Property 6/7). This is a deliberate deviation from "the 24.04 user-data contains the deltas" and is the reason for it: byte-identical jammy renderings (3.3, 3.8) are worth more than delta text being visible in the user-data string.

**Not changed**: `build_fleet.py` (`USER_DATA_BODY`/`render_user_data`/`resolve_ubuntu_ami`/`run_fleet_instance` all byte-identical — the launch path is already noble-AMI-capable and the bootstrap deltas arrive via the script, 3.8), `runner_bootstrap_user_data` text (byte-identical for all targets, 3.3), `plan_dedicated_dispatch` and the dedicated dispatch machinery (3.6), the state machine, readiness gate, preflight, and reconciliation.

## Testing Strategy

### Validation Approach

Two-phase: first run exploratory tests against the UNFIXED code to surface counterexamples confirming the bug condition and root cause; then implement the fix and verify with fix-checking and preservation property tests. All tests are offline (stubbed `boto3`/`shared_utils`, the established `test/backend-test/portal_builds/` harness conventions), Hypothesis-based where quantified, with `@settings(max_examples=100, deadline=None)` minimum per the suite's conventions. New tests live in `test/backend-test/portal_builds/` (e.g. `test_jp7_ephemeral_provisioning_exploration.py`, `test_jp7_ephemeral_provisioning_properties.py`, `test_jp7_dedicated_capability_properties.py`, `test_jp7_ephemeral_preservation.py`).

**On-host verification order (user preference, bugfix.md "Verification preference")**: candidate fixes are exercised on a **dedicated build machine FIRST** — a dedicated host retains docker layer caches across attempts, so fix iteration is substantially faster — and only then validated on the **ephemeral path**, where every attempt provisions a fresh runner with cold caches and exercises the full provision/bootstrap/readiness flow end-to-end. The offline unit/property suites under `test/backend-test/` remain the required merge gates in every case; the on-host order governs live verification only.

**What is verifiable offline, and what needs a host**: everything decided in Python — the planner's target→release derivation, the dispatcher's AMI resolution and fail-closed behavior, the runner record shape, and the capability gate — is covered by unit and property tests under `test/backend-test/portal_builds/` with stubbed `boto3`, so no instance is launched to test the fix's logic. The noble bootstrap **deltas** are likewise verified without launching anything, by script-level tests: rendering both user-data seams and asserting what they invoke and what they do not contain, and parsing `setup-build-server.sh` to assert its release branches (which commands fall under the detected-24.04 branch, which under 22.04, and that `bash -n` accepts the result). What those tests cannot establish is that a noble host with the deltas applied actually completes a JP7 build — that is the live verification above, dedicated machine first, then ephemeral.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If refuted, re-hypothesize.

**Test Plan**: Build JP7 Build_Job records and requests (the shapes `create_build_jobs`/`validate_build_request` produce and consume), run `plan_runner` / `resolve_ami` / `validate_build_request` on the unfixed code with recording stubs, and inspect the unfixed `setup-build-server.sh` / `USER_DATA_BODY` text — asserting the CORRECT (post-fix) expectations and observing the failures.

**Test Cases**:
1. **Plan carries no OS release**: `plan_runner(jp7_ephemeral_job)` — assert the plan distinguishes JP7's host OS from JP5's (will fail on unfixed code: RunnerPlan has no such field/value).
2. **Jammy AMI selected for JP7**: with a recording SSM stub and no env overrides, resolve the AMI for a JP7 plan — assert the requested parameter path contains `24.04` (will fail on unfixed code: the `22.04` arm64 parameter is read).
3. **Jammy env override applied to JP7**: with `BUILD_ARM64_AMI_ID` set to a jammy pin, resolve for JP7 — assert the jammy pin is NOT returned (will fail on unfixed code).
4. **No capability gate**: `validate_build_request` for JP7+dedicated selecting a running arm64 server with `ubuntu_version='22.04'` (and again with no `ubuntu_version` field) — assert rejection naming the capability (will fail on unfixed code: the request is accepted).
5. **Noble-incompatible shared bootstrap**: static assertions on the unfixed `setup-build-server.sh` — the three pip invocations carry no `--break-system-packages`, and the docker-compose section knows only the snap install (will fail on unfixed code); and `render_user_data(...)` contains only the plain script run with no release conditioning anywhere (documents 1.5/1.6).

**Expected Counterexamples**:
- Every JP7 ephemeral input planned/resolved identically to a JP5 input of the same snapshot: same RunnerPlan fields, same 22.04 parameter, same bootstrap text.
- Every JP7 dedicated request accepted against any running arm64 server regardless of recorded release; the launch bootstrap identical for 22.04 and 24.04.
- Possible causes confirmed: missing plan dimension, arch-only resolution, release-blind noble-incompatible shared bootstrap, missing capability gate.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed functions produce the expected behavior.

**Pseudocode:**
```
FOR ALL job WHERE isBugCondition(job) AND job.execution_mode = 'ephemeral' DO
  plan := plan_runner_fixed(job)
  ASSERT (plan.os_release, plan.arch) = ('24.04', 'arm64')
  ASSERT resolve_ami_fixed(plan.arch, plan.os_release) resolves via the
         noble arm64 parameter (ebs-gp3) or the hvm-ssd-gp3 DescribeImages
         fallback, honoring only BUILD_ARM64_NOBLE_AMI_ID as an override
END FOR

FOR ALL (os_release, arch) NOT IN mapped set DO
  ASSERT resolve_ami_fixed(arch, os_release) raises ValueError naming the
         pairing, with zero recorded AWS calls
END FOR

FOR ALL request WHERE request.targets CONTAINS 'JP7'
                  AND request.execution_mode = 'dedicated' DO
  s      := selectedServer(request)
  capable := s EXISTS AND s.lifecycle_state = 'running'
             AND s.arch = 'arm64'
             AND recordedRelease(s) = '24.04'   -- absent field => '22.04'
  result := validate_build_request_fixed(request, servers)
  ASSERT result.valid = capable                 -- accepts exactly the capable
  IF recordedRelease(s) != '24.04' THEN
    ASSERT some error has rule RULE_SERVER_OS_RELEASE_MISMATCH
           AND its message names Ubuntu 24.04 arm64 AND the actual release
  END IF
  -- and every other failing condition still reports its own existing rule
  -- rejection happens at validation: no job record is ever created
END FOR

ASSERT setup-build-server.sh (fixed): under the detected-24.04 branch,
       the docker-compose shim (exec docker compose "$@") is written and
       the snap docker-compose install is unreachable; all three pip
       installs carry --break-system-packages via the release-keyed flag
ASSERT both user-data seams (render_user_data body, runner_bootstrap
       build-user body) execute setup-build-server.sh and contain NO
       second copy of any noble delta
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed functions produce the same result as the original functions.

**Pseudocode:**
```
FOR ALL job WHERE NOT isBugCondition(job) DO
  ASSERT plan_runner_original(job) fields = plan_runner_fixed(job) fields
         (arch, instance_type, volume_size_gb, spot, status)
  ASSERT resolve_ami_original(arch) = resolve_ami_fixed(arch)   -- default release
END FOR

FOR ALL job (ANY target) DO
  ASSERT runner_bootstrap_user_data_original(job, dir)
         = runner_bootstrap_user_data_fixed(job, dir)           -- byte-identical
END FOR

FOR ALL (repo_url, repo_dir, source_ref) DO
  ASSERT render_user_data_original(...) = render_user_data_fixed(...)  -- byte-identical
END FOR

FOR ALL request WHERE NOT (JP7 IN targets AND execution_mode = 'dedicated') DO
  ASSERT validate_build_request_original(request, servers)
         = validate_build_request_fixed(request, servers)
END FOR
-- including: JP5/JP6 dedicated on servers with ubuntu_version 22.04,
-- 24.04, or absent -> accepted exactly as today (3.9)
```

**Testing Approach**: Property-based testing is used for preservation because it generates many job/request/server shapes (snapshots, refs, overrides, `ubuntu_version` values including absence) automatically, catches edge cases manual tests would miss, and gives strong guarantees that non-JP7 and 22.04 behavior is unchanged. The "original" side is captured as frozen oracles observed on the UNFIXED code (parameter paths, resolution order, bootstrap text structure, validation outcomes), re-spelled in the tests in the suite's frozen-oracle style (`FROZEN_MATRIX` precedent) rather than by importing the old code.

**Test Cases**:
1. **Plan preservation**: observe `plan_runner` field outputs for generated non-JP7 jobs on unfixed code; property test asserts identical fields after the fix (and that `os_release` equals the default for those targets).
2. **AMI resolution preservation**: observe the override→cache→22.04-parameter order on unfixed code with a recording stub; property test asserts the identical order, parameters, and cache behavior after the fix, for both architectures and all override configurations.
3. **Bootstrap byte-identity (both seams)**: observe `runner_bootstrap_user_data` text for generated jobs/refs/dirs and `render_user_data` text for generated repo/dir/ref inputs on unfixed code; property tests assert byte-equality after the fix (all targets for the runner seam — the deltas live in the script, not the user-data).
4. **Validation outcome preservation and release-invariance**: over generated requests and fleets (targets, modes, server states, arch values, `ubuntu_version` present/absent), every combination other than JP7+dedicated keeps its exact accept/reject outcome and error rules (3.5); and in the paired form of Property 8, two fleets differing ONLY in the selected server's recorded `ubuntu_version` yield identical outcomes for every non-JP7 request — so JP5/JP6/AMD64/AMD64_NVIDIA dedicated requests are accepted on a noble server and on a jammy or field-less server alike (3.9).
5. **22.04 script behavior**: static/structural assertions that the jammy paths of `setup-build-server.sh` are intact — the snap docker-compose branch unchanged, the release-keyed flag expanding empty on 22.04 so the pip command lines are today's (3.8) — plus `bash -n` syntax validation.
6. **Full-suite gate (3.7)**: the complete existing `test/backend-test/` offline suites run unmodified and pass.

### Unit Tests

- `required_os_release_for_target`: the five supported targets map to the frozen release table; unsupported targets raise `ValueError`.
- `resolve_ami` noble branch: SSM-parameter success, DescribeImages fallback on SSM failure, `BUILD_ARM64_NOBLE_AMI_ID` override precedence, noble cache hit, no-image diagnostic.
- Unmapped-pairing error message names both release and arch; `provision_ephemeral` converts it into a failed job with `ERROR_PROVISIONING_FAILED` (stubbed persistence), never crashing the tick.
- Capability gate: JP7+dedicated rejected against `ubuntu_version='22.04'`, `'20.04'`, and ABSENT (pre-ec1dc38 record); accepted against `'24.04'`; the diagnostic names both the required capability and the actual release; the gate composes correctly with existing rules (not-found/not-running/arch-mismatch rejections still fire and are not masked).
- `setup-build-server.sh` structure: release detection present; shim block (`exec docker compose "$@"`, `chmod +x`) under the 24.04 branch only; `--break-system-packages` reaches exactly the three PEP 668-affected installs via the release-keyed flag; `run_cmd`/summary conventions kept.
- Jammy env overrides do not apply to noble resolution; noble override does not apply to jammy resolution.

### Property-Based Tests

- Property 1 (frozen release/arch oracle over all targets; noble resolution for JP7) — min 100 examples.
- Property 2 (fail-closed over generated unmapped pairings, zero AWS calls) — min 100 examples.
- Property 3 (non-JP7 plan + AMI resolution preservation over generated jobs/overrides) — min 100 examples.
- Property 4 (bootstrap byte-identity over generated jobs of ALL targets) — min 100 examples.
- Property 5 (capability gate accepts iff running AND arm64 AND recorded release `'24.04'`, over generated server records with `lifecycle_state`/`arch`/`ubuntu_version` varied and absent; diagnostic contents; no job record on rejection) — min 100 examples.
- Property 6 (single shared delta implementation: both generated user-data seams execute the script and carry no delta text, over generated repo/dir/ref inputs and all targets; noble branch of the script carries all four deltas) — min 100 examples.
- Property 7 (22.04 byte-identity of `render_user_data` over generated inputs; jammy command sequence of the script unchanged) — min 100 examples.
- Property 8 (`ubuntu_version`-invariance of non-JP7 validation outcomes: paired fleets differing only in the recorded release, over generated non-JP7 requests in both modes) — min 100 examples.

### Integration Tests

- Dispatcher-tick style test (existing `test_dispatcher_tick_integration.py` conventions): a queued JP7 ephemeral job is provisioned with the noble AMI id returned by the stub, the runner record carries `os_release='24.04'`, and the readiness/agent flow proceeds unchanged.
- Mixed-batch tick: JP5 + JP7 ephemeral jobs in one tick — each provisioned from its own release's AMI, jammy jobs byte-preserved.
- Unmapped-pairing tick: a synthetic plan with an unmapped pairing fails exactly one job with the naming diagnostic; other jobs in the batch are unaffected.
- Request-to-rejection flow: a JP7+dedicated submission selecting a jammy (or pre-ec1dc38) server is rejected at the API validation boundary with the capability diagnostic and NO job record is created; the same submission against a 24.04 arm64 server is accepted and dispatches through the unchanged dedicated machinery (allocation, pgrep pre-dispatch verification) — 3.6.
- Live verification (manual, per the user's preference): exercise the candidate fix on a dedicated 24.04 build machine first (retained docker layer caches, fast iteration: launch via the fleet page, confirm the bootstrap needs no manual steps, run a JP7 dedicated build), then validate the ephemeral path end-to-end (fresh noble runner, cold caches, full provision/bootstrap/build/publish flow).
