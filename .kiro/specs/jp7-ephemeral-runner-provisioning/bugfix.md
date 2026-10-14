# Bugfix Requirements Document

## Introduction

Portal builds for the JP7 build target fail in **both execution modes** (ephemeral and dedicated). JP7 builds require an Ubuntu 24.04 (noble) arm64 build host (jetpack7-support spec, README section 4.2), including noble-specific host deltas such as the `docker-compose` command shim (noble ships only the `docker compose` plugin, while `build-custom.sh` invokes `docker-compose`) and PEP 668-compatible pip installs (noble's Python is externally managed, so user-level installs need `--break-system-packages`).

**Ephemeral mode**: build job validation (`build_domain.py`) accepts JP7 in the ephemeral execution mode, and the dispatcher (`edge-cv-portal/backend/functions/build_dispatcher.py`) provisions the Ephemeral_Build_Runner via `resolve_ami(arch)`, which selects an Ubuntu 22.04 (jammy) AMI keyed only by CPU architecture (arm64/x86_64). The runner plan (`build_planner.plan_runner` / `RunnerPlan`) carries only the CPU architecture derived from the build target; neither the plan nor the dispatcher's AMI resolution has any awareness of the OS release a target requires. A JP7 ephemeral job is therefore provisioned onto a jammy arm64 runner and the build fails mid-run.

**Dedicated mode**: the fleet launch path (`build_fleet.py`, commit ec1dc38) can resolve a noble arm64 AMI when `ubuntu_version=24.04` is requested, but the launch renders the same `USER_DATA_BODY` bootstrap for every release — a plain `setup-build-server.sh` run with none of the noble deltas README 4.2 documents. README 4.2's own note confirms the gap: a portal-launched 24.04 server requires "additionally apply steps 2–3 above on the launched server (the `docker-compose` shim in particular)" before it is JP7-build-capable. Worse, `setup-build-server.sh` is actively noble-incompatible: its `sudo pip3 install awscli`, `sudo pip3 install 'botocore[crt]'`, and `pip3 install --user git+...gdk-cli` invocations lack `--break-system-packages` and fail under noble's PEP 668 externally-managed Python. Independently, JP7 dedicated dispatch has no capability gate: `build_domain.validate_build_request` checks only that the selected server exists, is running, and matches the required arch (arm64), and `build_planner.plan_dedicated_dispatch` targets exactly the selected server — so a jammy 22.04 arm64 server accepts a JP7 dedicated job and fails mid-build the same way as the ephemeral path. The jetpack7-support requirements (Req 6.4/6.5: missing-capability rejection with a diagnostic before any build work starts) are not implemented in this path. The server record does carry `ubuntu_version` since commit ec1dc38 (servers launched before that change carry no field and are 22.04 hosts), so a capability gate has authoritative data to key on.

**Bug Condition C(X)**: a build job with `build_target = JP7` is dispatched, in either execution mode, onto a host that lacks the noble JP7 host prerequisites — for ephemeral, the runner is provisioned from the wrong OS release (jammy); for dedicated, the selected server is either a portal-launched 24.04 server missing the noble bootstrap deltas, or a wrong-OS (22.04 jammy) registered server that validation and dispatch never screened out.

**Fix intent**: make JP7 builds work in both modes. Ephemeral: provision an Ubuntu 24.04 (noble) arm64 runner — target-aware AMI resolution plus the noble bootstrap deltas — failing closed on unmappable OS-release/architecture pairings. Dedicated: a portal-launched 24.04 fleet server must come up JP7-build-capable without manual steps (the bootstrap applies the noble deltas), and JP7 dedicated dispatch must not select a host that cannot run the build (gate on the recorded `ubuntu_version` — a jammy host can never run a JP7 build regardless of applied deltas, so the noble deltas alone cannot substitute for the gate). The noble deltas apply through the bootstrap seams shared by both modes (`setup-build-server.sh` and the dispatcher's runner bootstrap user-data).

**Decision point (fallback)**: if provisioning noble ephemeral runners proves infeasible during design or implementation, the fallback is to reject JP7 + ephemeral at validation time with a diagnostic directing the user to the dedicated execution mode. The primary path (clauses 2.1–2.4 below) is written for the provisioning fix; the fallback replaces clauses 2.1–2.4 with an up-front rejection only if that decision is taken. The dedicated-mode clauses (2.6–2.8) stand in either case.

**Verification preference**: candidate fixes are exercised on a dedicated build machine first — a dedicated host retains docker layer caches across attempts, making fix iteration substantially faster — and then validated on the ephemeral path (where every attempt provisions a fresh runner with cold caches).

## Bug Analysis

### Current Behavior (Defect)

When a JP7 build job is dispatched in the ephemeral execution mode, the runner plan and AMI resolution ignore the OS release the target requires:

1.1 WHEN a build job with build_target JP7 and execution_mode ephemeral is dispatched THEN the system provisions the Ephemeral_Build_Runner from the Ubuntu 22.04 (jammy) arm64 AMI selected by `resolve_ami('arm64')`, which is the wrong OS release for the JP7 build

1.2 WHEN a JP7 ephemeral build runs on the jammy runner THEN the system fails mid-run during the build (the noble-only toolchain and host prerequisites required by the JP7 build, per jetpack7-support and README 4.2, are absent) instead of failing fast or succeeding

1.3 WHEN the dispatcher builds the provisioning plan for a JP7 ephemeral job THEN `build_planner.plan_runner` produces a RunnerPlan carrying only the CPU architecture (arm64), with no representation of the required OS release, so the dispatcher has no information with which to select a noble AMI

1.4 WHEN the runner bootstrap (runner_bootstrap_user_data / setup-build-server.sh) runs on the provisioned runner for a JP7 job THEN the system applies only the jammy-oriented setup, without the noble host deltas documented in README 4.2 (such as the `docker-compose` command shim for the noble `docker compose` plugin invoked by `build-custom.sh` as `docker-compose`)

When a JP7 build job is dispatched in the dedicated execution mode, neither the fleet launch nor the dispatch capability check produces a host that can run the build:

1.5 WHEN a Dedicated_Build_Server is launched from the portal fleet page with ubuntu_version 24.04 THEN the system renders the same user-data bootstrap as a 22.04 launch (`build_fleet.USER_DATA_BODY`, a plain `setup-build-server.sh` run) without any of the noble deltas README 4.2 documents, so the launched server is registered in the fleet but is NOT JP7-build-capable without manual operator steps (README 4.2's note: "additionally apply steps 2–3 above on the launched server")

1.6 WHEN `setup-build-server.sh` runs on an Ubuntu 24.04 (noble) host THEN the system fails or degrades the bootstrap: its `sudo pip3 install awscli`, `sudo pip3 install 'botocore[crt]'`, and `pip3 install --user git+...aws-greengrass-gdk-cli` invocations lack `--break-system-packages` and are rejected by noble's PEP 668 externally-managed Python, and it installs `docker-compose` via snap instead of providing the documented shim delegating to the noble `docker compose` plugin

1.7 WHEN a build request with build_target JP7 and execution_mode dedicated selects a running arm64 server on Ubuntu 22.04 (jammy) THEN the system accepts the request (`build_domain.validate_build_request` checks only server existence, running lifecycle state, and CPU architecture — never the recorded `ubuntu_version` or any JP7 capability) and the build fails mid-run on the jammy host exactly as the ephemeral path does

1.8 WHEN the dispatcher plans a dedicated dispatch for a JP7 job THEN `build_planner.plan_dedicated_dispatch` targets exactly the selected server with no capability screen, so no missing-capability diagnostic is produced before build work starts (contrary to jetpack7-support Req 6.4/6.5) and the failure surfaces only mid-build

### Expected Behavior (Correct)

When a JP7 build job is dispatched in the ephemeral execution mode, the runner must be provisioned on the OS release the target requires:

2.1 WHEN a build job with build_target JP7 and execution_mode ephemeral is dispatched THEN the system SHALL provision the Ephemeral_Build_Runner from an Ubuntu 24.04 (noble) arm64 AMI, resolved following the conventions established by `build_fleet.resolve_ubuntu_ami` (the canonical noble arm64 SSM parameter with its ebs-gp3 path segment, and the Canonical-owner DescribeImages fallback with the hvm-ssd-gp3 noble name filter)

2.2 WHEN the dispatcher builds the provisioning plan for a JP7 ephemeral job THEN the runner plan SHALL carry the OS release required by the job's build target (derived per target alongside `required_arch`, with Ubuntu 22.04 as the default for all existing targets), so AMI resolution is target-aware

2.3 WHEN AMI resolution is requested for an OS-release/architecture pairing that has no mapping (for example noble on x86_64) THEN the system SHALL fail closed before any AWS call, fail the build job with a provisioning error naming the unmapped pairing, and SHALL NOT provision a runner from a fallback AMI of a different OS release

2.4 WHEN the runner bootstrap runs on a noble runner provisioned for a JP7 ephemeral job THEN the system SHALL apply the noble host deltas required for the build to complete (per README 4.2, including a `docker-compose` command shim so `build-custom.sh`'s `docker-compose` invocations resolve, and PEP 668-compatible package installs), such that the JP7 ephemeral build can proceed past host setup

2.5 IF provisioning noble ephemeral runners is determined to be infeasible (fallback decision) THEN the system SHALL reject a build request combining build_target JP7 with execution_mode ephemeral at validation time, before any job is created or any provisioning starts, with a diagnostic naming the unsupported combination and directing the user to the dedicated execution mode

When a JP7 build job is dispatched in the dedicated execution mode, the fleet launch must produce a capable host and dispatch must not select an incapable one:

2.6 WHEN a Dedicated_Build_Server is launched from the portal fleet page with ubuntu_version 24.04 THEN the system SHALL produce a JP7-build-capable server without manual operator steps: the launch bootstrap (`setup-build-server.sh` and/or the user-data that invokes it) SHALL apply the noble deltas of README 4.2 on a noble host (the `docker-compose` shim delegating to the `docker compose` plugin, and PEP 668-compatible installs of the AWS CLI, `botocore[crt]`, and the GDK CLI), so that a JP7 dedicated build dispatched to that server completes host setup

2.7 WHEN a build request with build_target JP7 and execution_mode dedicated selects a server whose recorded `ubuntu_version` is not 24.04 (including servers launched before commit ec1dc38, which carry no `ubuntu_version` field and are 22.04 hosts) THEN the system SHALL reject the request at validation time with a diagnostic naming the missing JP7 host capability (Ubuntu 24.04 arm64) and the server's actual release, before any job is created (jetpack7-support Req 6.4/6.5 fail-closed semantics); the gate SHALL key on the recorded `ubuntu_version` because a 22.04 (jammy) host cannot run a JP7 build regardless of which bootstrap deltas are applied to it

2.8 WHEN the noble host deltas are implemented THEN the system SHALL apply them through the bootstrap seams shared by both execution modes (`setup-build-server.sh` and the dispatcher's runner bootstrap user-data), so the ephemeral runner bootstrap (2.4) and the dedicated fleet launch bootstrap (2.6) share one implementation of the noble deltas rather than two divergent copies

### Unchanged Behavior (Regression Prevention)

All non-JP7 behavior in both modes, and all 22.04 behavior, must remain byte-identical:

3.1 WHEN a build job with build_target JP5, JP6, AMD64, or AMD64_NVIDIA and execution_mode ephemeral is dispatched THEN the system SHALL CONTINUE TO provision the runner from the same Ubuntu 22.04 AMI resolution as today (the per-architecture canonical 22.04 SSM parameters), producing the identical AMI selection for identical inputs

3.2 WHEN the BUILD_ARM64_AMI_ID or BUILD_X86_64_AMI_ID environment overrides are set THEN the system SHALL CONTINUE TO honor the explicit AMI id ahead of any SSM parameter lookup for the targets those overrides cover today, with the existing per-container cache behavior preserved

3.3 WHEN the runner bootstrap runs for a JP5, JP6, AMD64, or AMD64_NVIDIA ephemeral job THEN the system SHALL CONTINUE TO emit the existing bootstrap content unchanged (no noble deltas applied to jammy runners)

3.4 WHEN `build_planner.plan_runner` plans a runner for a JP5, JP6, AMD64, or AMD64_NVIDIA ephemeral job THEN the system SHALL CONTINUE TO derive the same architecture, instance type, volume size, spot flag, and status from the job's build target and config_snapshot as today

3.5 WHEN a build request is validated for any currently accepted target/execution-mode combination other than JP7 (either mode) THEN the system SHALL CONTINUE TO accept or reject it exactly as today (no previously accepted combination becomes rejected, and no previously rejected combination becomes accepted)

3.6 WHEN a JP7 build job is dispatched in the dedicated execution mode to a capable (noble arm64) server THEN the system SHALL CONTINUE TO route it through the existing dedicated dispatch machinery unchanged (exact-selected-server allocation per Req 2.2, single running slot per Req 7.1, queueing per Req 7.2, pre-dispatch pgrep verification per Req 7.5/7.6) — the fix adds only the capability gate of 2.7 in front of that machinery

3.7 WHEN the existing offline test suites under `test/backend-test/` (including `test/backend-test/portal_builds/`) run against the fixed code THEN the system SHALL CONTINUE TO pass every existing test without modification to the tests' expected values for non-JP7 behavior

3.8 WHEN a Dedicated_Build_Server is launched from the portal fleet page with ubuntu_version 22.04 (or with no ubuntu_version, the default) THEN the system SHALL CONTINUE TO render the launch bootstrap byte-identically to today (the same user-data content and the same `setup-build-server.sh` behavior on a jammy host), and `setup-build-server.sh` run on a 22.04 host SHALL CONTINUE TO produce the same build environment as today

3.9 WHEN a build request with build_target JP5 or JP6 and execution_mode dedicated selects any running arm64 server THEN the system SHALL CONTINUE TO accept and dispatch it exactly as today, regardless of the server's `ubuntu_version` (the capability gate of 2.7 applies to JP7 only)
