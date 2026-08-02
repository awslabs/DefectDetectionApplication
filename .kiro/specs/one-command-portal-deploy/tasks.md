# Implementation Plan: One-Command Portal Deploy

## Overview

The deliverable is a single new bash orchestrator, `edge-cv-portal/deploy-portal.sh`, that **invokes** the five existing scripts (`deploy-account-role.sh`, `infrastructure/deploy-auth.sh`, `deploy-infrastructure.sh`, `deploy-frontend.sh`, `configure-bucket-cors.sh`) in order without modifying any of them, plus a `bats` test suite. The plan builds from the inside out so every prompt lands on already-integrated ground:

1. **Harness first.** Create the script skeleton and the `bats` stub harness (PATH-shadow stubs that record invocation order, `$PWD`, args, env, and stdin) so every subsequent function is test-driven and runs offline with no AWS account.
2. **Pure functions next.** Input resolution (flag-over-env), topology validation, non-interactive input aggregation + External-ID reuse reconciliation — all pure, echo-only, and directly property-testable.
3. **Prerequisite gate and step runner** — the gating and logging/redaction/CWD machinery.
4. **Account-role invocation** (interactive TTY-preserving tee vs. non-interactive constructed-stdin feed).
5. **Orchestration** (per-topology ordered step sequencing + stop-on-failure) then the **Deployment_Summary** (Portal URL, role ARNs, External IDs, per-step outcomes).
6. **Property, interactive (pty), and integration tests** last where they depend on wired behavior.

`deploy-portal.sh` is bash tested with **`bats`**. Property tests are **generator-driven `bats` tests** running a **minimum of 100 generated cases each**, tagged `# Feature: one-command-portal-deploy, Property {N}: {title}`. This orchestrator targets portal/infra deployment, not an on-device edge feature, so hardware/device testing is out of scope; the orchestrator's own steps must remain idempotent and resumable (Req 6.3), which the re-run equivalence property verifies. Because acceptance is verified via `bats` with stubbed AWS, no task requires a live AWS account. Sub-tasks marked with `*` are optional test tasks and are not implemented during core delivery.

## Tasks

- [x] 1. Create the orchestrator skeleton and the bats stub harness
  - [x] 1.1 Create the `deploy-portal.sh` skeleton
    - Create `edge-cv-portal/deploy-portal.sh` with `set -euo pipefail`, a shebang, function placeholders for every design component (`resolve_input`, `parse_args`, `validate_topology`, `required_inputs_for`, `check_required_inputs`, `check_prerequisites`, `run_step`, `resolve_portal_url`, `collect_role_arns`, `print_summary`), a `--help`/usage block documenting the CLI surface, and a `main` dispatcher; make it executable
    - Define the `STEPS_FOR` topology→ordered-step map and the `STEP_OUTCOMES` associative array initialized to `not-run`
    - _Requirements: 1.1_
  - [x] 1.2 Build the bats PATH-shadow stub harness
    - Create `edge-cv-portal/tests/` with a `bats` setup helper that builds a temp `bin/` prepended to `PATH` containing stub executables for `deploy-account-role.sh`, `deploy-auth.sh`, `deploy-infrastructure.sh`, `deploy-frontend.sh`, `configure-bucket-cors.sh` and for `aws`, `node`, `cdk`, `npx`
    - Each stub appends to a trace file its name/order, `$PWD`, positional args, selected env vars, and any stdin it received, and emits caller-controlled stdout/stderr and exit codes (driven by env fixtures)
    - Provide helpers to seed a fake `edge-cv-portal` working tree, fake `*-config.txt` files, and canned `aws` responses (sts identity, ssm bootstrap version, cloudformation describe-stacks outputs)
    - _Requirements: 8.1, 8.3_
  - [ ]* 1.3 Add the property-test generator helper
    - Add a `bats` helper that generates randomized inputs (valid/invalid topology strings, distinct flag/env value pairs, arbitrary omitted-input subsets, random failure-step indices, random bootstrap version values, random secret sentinels, random step output) and a loop harness that runs each property over ≥100 generated cases
    - _Requirements: 8.1_

- [x] 2. Implement argument and environment resolution
  - [x] 2.1 Implement `resolve_input` and `parse_args`
    - Implement `resolve_input <flag> <env>` echoing the flag value when non-empty else the env value (flag-over-env precedence); implement `parse_args` for the full CLI surface (positional topology, `--non-interactive`, `--portal-account-id`, `--external-id`, `--data-buckets`, `--sso`, `--profile`, `--region`, `--log-file`, `--help`) with each flag's env fallback (`PORTAL_DEPLOY_TOPOLOGY`, `PORTAL_DEPLOY_NON_INTERACTIVE`, `PORTAL_ACCOUNT_ID`, `EXTERNAL_ID`, `DATA_BUCKET_NAMES`, `PORTAL_DEPLOY_SSO`, `AWS_PROFILE`, `AWS_REGION`/`AWS_DEFAULT_REGION`)
    - Export the frontend passthrough env (`TRUSTED_USECASE_ACCOUNT_IDS`, `DATA_BUCKET_ALLOWLIST`, `cloudFrontDomain`) and SSO env (`SSO_METADATA_URL`, `SSO_PROVIDER_NAME`, `COGNITO_DOMAIN_PREFIX`) unchanged
    - _Requirements: 5.1, 5.5, 5.6, 8.4_
  - [ ]* 2.2 Write property test for flag-over-env precedence
    - **Feature: one-command-portal-deploy, Property 8: Flag values take precedence over environment values**
    - **Validates: Requirements 5.5**
  - [ ]* 2.3 Write unit tests for argument parsing and passthrough
    - Verify each flag/env pair resolves correctly, unknown flags error, `--help` prints usage, and passthrough/SSO env vars are exported unmodified
    - _Requirements: 5.1, 5.6, 8.4_

- [x] 3. Implement topology validation
  - [x] 3.1 Implement `validate_topology`
    - Implement `validate_topology <value>` returning success for `single-account`/`usecase`/`data`; otherwise write a message to stderr naming the invalid value and exit non-zero, wired before any step runs
    - _Requirements: 4.3_
  - [ ]* 3.2 Write property test for invalid-topology rejection
    - **Feature: one-command-portal-deploy, Property 6: Invalid topologies are rejected before any step**
    - **Validates: Requirements 4.3**

- [x] 4. Implement non-interactive input validation and External-ID reuse reconciliation
  - [x] 4.1 Implement `required_inputs_for` and `check_required_inputs`
    - Implement `required_inputs_for <topology>` (single-account: none beyond topology; usecase/data: `PORTAL_ACCOUNT_ID`, `EXTERNAL_ID` unless reusable config exists; data buckets optional) and `check_required_inputs`, which in Non_Interactive_Mode collects every missing/empty required input and reports them all in a single failure message, running no mutating step and exiting non-zero
    - _Requirements: 5.1, 5.3_
  - [x] 4.2 Implement External-ID reuse reconciliation
    - Stat the same config file the script reads (`usecase-account-<acct>-config.txt` / `data-account-<acct>-config.txt` in the `edge-cv-portal` CWD, `<acct>` = resolved account); when present and `--external-id`/`EXTERNAL_ID` was not supplied, treat External ID as not required and do not override it; when supplied, decline reuse
    - _Requirements: 2.4, 5.3_
  - [ ]* 4.3 Write property test for complete missing-input reporting
    - **Feature: one-command-portal-deploy, Property 7: Non-interactive missing inputs are reported completely with no mutating step**
    - **Validates: Requirements 5.3**
  - [ ]* 4.4 Write unit test for External-ID reuse reconciliation
    - With a fake `usecase-account-<acct>-config.txt` present and no `--external-id`, assert External ID is not required and not overridden; with `--external-id` supplied, assert reuse is declined
    - _Requirements: 2.4_

- [x] 5. Implement the prerequisite gate and CDK bootstrap
  - [x] 5.1 Implement `check_prerequisites`
    - Run checks in order before any mutating step: `command -v aws`; `aws sts get-caller-identity` returns a non-empty non-`None` account; region resolved via `--region`→`AWS_REGION`→`AWS_DEFAULT_REGION`→`aws configure get region`→`CDK_DEFAULT_REGION`; `command -v node` and `cdk`/`npx cdk`; on any failure write which check failed to stderr, run no step, exit non-zero; on success export `AWS_REGION`/`AWS_DEFAULT_REGION`/`CDK_DEFAULT_REGION`/`CDK_DEFAULT_ACCOUNT` and print resolved account id + region before the first mutating step
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.8_
  - [x] 5.2 Implement the bootstrap version check and auto-bootstrap
    - Read `aws ssm get-parameter --name /cdk-bootstrap/hnb659fds/version`; if absent or numerically `< 21`, run `cdk bootstrap aws://<account>/<region>` before any CDK-deploying step; if bootstrap exits non-zero, write "CDK bootstrap failed" to stderr, run no CDK-deploy step, exit non-zero
    - _Requirements: 3.6, 3.7_
  - [ ]* 5.3 Write property test for the prerequisite gate
    - **Feature: one-command-portal-deploy, Property 3: The prerequisite gate blocks all mutating steps on any failure**
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.7**
  - [ ]* 5.4 Write property test for the bootstrap threshold
    - **Feature: one-command-portal-deploy, Property 4: Bootstrap runs exactly when the version is missing or below 21**
    - **Validates: Requirements 3.6**
  - [ ]* 5.5 Write unit tests for prerequisite messages and context print
    - Absent `aws` (3.2), invalid `sts` identity (3.3), unresolved region (3.4), absent `node`/`cdk` naming the tool (3.5), failed bootstrap (3.7), and resolved account id + region printed before the first step (3.8)
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 3.7, 3.8_

- [x] 6. Implement the step runner with logging, redaction, and working-dir handling
  - [x] 6.1 Implement `run_step` and the redaction/log pipeline
    - Create the Deployment_Log (`edge-cv-portal/deploy-portal-<UTC-timestamp>.log` default, `--log-file` override) and print its path before the first step; implement `run_step <name> <cwd> <interactive?> [stdin_feed] -- <command...>` that prints the step name to stdout, runs the command from `<cwd>`, pipes stdout+stderr through a `sed`/`awk` redaction filter (masking `AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN`/`aws_secret_access_key`/`aws_session_token`) into `tee -a` the log, records the outcome in `STEP_OUTCOMES`, and performs no rollback on failure
    - _Requirements: 6.2, 6.4, 6.5, 6.6, 7.4, 8.3_
  - [ ]* 6.2 Write property test for step announcement and output capture
    - **Feature: one-command-portal-deploy, Property 11: Every step's output is captured in the log and its name is announced**
    - **Validates: Requirements 6.4, 6.5**
  - [ ]* 6.3 Write property test for secret redaction
    - **Feature: one-command-portal-deploy, Property 12: Credential secret material never appears in output, log, or summary**
    - **Validates: Requirements 7.4**
  - [ ]* 6.4 Write property test for per-step working directory
    - **Feature: one-command-portal-deploy, Property 16: Each step runs from the working directory it expects**
    - **Validates: Requirements 8.3**
  - [ ]* 6.5 Write unit test for log-path announcement
    - Assert the Deployment_Log location is printed to stdout before the first step banner
    - _Requirements: 6.6_

- [x] 7. Checkpoint - pure functions, prerequisite gate, and step runner complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Implement account-role invocation (interactive and non-interactive)
  - [x] 8.1 Implement interactive account-role invocation
    - Invoke `run_step "account-role" edge-cv-portal true -- ./deploy-account-role.sh "$TOPOLOGY"` with a TTY-preserving tee (process substitution) so the script's `read -p` prompts reach and read from the terminal while output is copied (redacted) to the log; pass the topology positionally without reimplementing any prompt
    - _Requirements: 2.1, 2.2, 4.1, 4.2_
  - [x] 8.2 Implement non-interactive constructed-stdin feed
    - Build the stdin feed matching the script's prompt order per topology (usecase/data new config: account id then external id; existing config with external-id supplied: `n` then value; existing config without: `Y` to reuse; data appends bucket names, possibly blank; single-account: no lines) and pipe it in so no terminal prompting occurs; invoke via the positional-argument path
    - _Requirements: 4.2, 5.2, 5.4, 2.4_
  - [ ]* 8.3 Write property test for topology positional passthrough
    - **Feature: one-command-portal-deploy, Property 5: The selected topology is passed to account-role as its positional argument**
    - **Validates: Requirements 4.2, 5.4**
  - [ ]* 8.4 Write pty interactive tests
    - Using an `expect`/pty harness: interactive topology selection through the account-role menu (4.1), operator responses flowing through to the stubbed script (2.2), and non-interactive mode with stdin from `/dev/null` never blocking and emitting no orchestrator prompts (5.1, 5.2)
    - _Requirements: 4.1, 2.2, 5.1, 5.2_
  - [ ]* 8.5 Write unit test that account-role is invoked, not reimplemented
    - Assert `deploy-portal.sh` contains no duplicated `read -p` prompts for account/external-id and that the account-role stub is the source of prompting
    - _Requirements: 2.1_

- [x] 9. Implement per-topology orchestration and stop-on-failure sequencing
  - [x] 9.1 Wire the ordered step sequence for all topologies
    - Implement the main orchestration loop driving `STEPS_FOR`: single-account runs account-role → auth (`edge-cv-portal/infrastructure`, with `--sso`/`--profile`/`--region`) → infrastructure (`edge-cv-portal`) → frontend (`edge-cv-portal`, with passthrough env exported); usecase/data run only account-role and never auth/infrastructure/frontend; stop before the next step on any non-zero exit, print the failed step name + log path, and exit non-zero with remaining steps left `not-run`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 4.4, 6.1, 6.2, 8.4_
  - [ ]* 9.2 Write property test for topology-driven ordered step sequence
    - **Feature: one-command-portal-deploy, Property 1: Topology determines the exact ordered step sequence**
    - **Validates: Requirements 1.1, 1.2, 1.4, 4.4**
  - [ ]* 9.3 Write property test for all-success exit code
    - **Feature: one-command-portal-deploy, Property 2: All steps succeeding yields a zero exit code**
    - **Validates: Requirements 1.3**
  - [ ]* 9.4 Write property test for stop-on-failure reporting
    - **Feature: one-command-portal-deploy, Property 9: A failing step stops the sequence and reports the failed step and log location**
    - **Validates: Requirements 6.1**
  - [ ]* 9.5 Write property test for frontend passthrough preservation
    - **Feature: one-command-portal-deploy, Property 17: Frontend passthrough values are preserved exactly**
    - **Validates: Requirements 8.4**
  - [ ]* 9.6 Write property test for orchestrated-script immutability
    - **Feature: one-command-portal-deploy, Property 15: Orchestrated script files are never modified**
    - **Validates: Requirements 8.1**

- [x] 10. Implement output collection and the Deployment_Summary
  - [x] 10.1 Implement `resolve_portal_url`, `collect_role_arns`, and `print_summary`
    - Implement `resolve_portal_url` (CloudFormation `DistributionDomainName` output of `EdgeCVPortalFrontendStack` → `https://<domain>` or empty); `collect_role_arns <topology> <account>` (single-account: `DDASageMakerExecutionRole` + `Greengrass_ServiceRole` ARNs; usecase/data: `ROLE_ARN` + `EXTERNAL_ID` read from the `*-config.txt`); `print_summary` emitting topology, account, region, Portal URL (single-account), created role ARNs, External IDs (usecase/data), and every defined step's outcome as exactly one of success/failure/not-run — assembled only from CloudFormation outputs and config files, never credential material; if single-account and the Portal URL is unresolvable after frontend, state it is unavailable, include the log path, and exit non-zero
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_
  - [ ]* 10.2 Write property test for per-step outcome reporting
    - **Feature: one-command-portal-deploy, Property 13: The summary reports each defined step's outcome exactly once**
    - **Validates: Requirements 7.5**
  - [ ]* 10.3 Write property test for summary identifiers
    - **Feature: one-command-portal-deploy, Property 14: The summary surfaces the Portal URL and created identifiers**
    - **Validates: Requirements 7.1, 7.2, 7.3**
  - [ ]* 10.4 Write property test for re-run equivalence
    - **Feature: one-command-portal-deploy, Property 10: A fully successful re-run is equivalent to a first-attempt success**
    - **Validates: Requirements 6.3**
  - [ ]* 10.5 Write unit tests for URL-unavailable and SSO passthrough
    - Portal URL unavailable after frontend → summary indicates unavailable + log path + non-zero exit (7.6); `--sso` path → auth stub receives `--sso` with `SSO_*` env preserved (5.6)
    - _Requirements: 7.6, 5.6_

- [x] 11. Checkpoint - full orchestration and summary wired
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Add the optional npm alias and integration tests
  - [x] 12.1 Add the optional npm alias passthrough
    - If a portal-root `package.json` exists, add a thin `"deploy:portal": "./edge-cv-portal/deploy-portal.sh"` `scripts` entry that only passes through to the bash script with no behavior of its own; otherwise document the alias without introducing a new manifest
    - _Requirements: 1.1_
  - [ ]* 12.2 Write the standalone-equivalence integration test
    - Run each orchestrated script standalone against the mocked AWS surface and compare exit code, created/modified files, and mock-recorded AWS calls to a baseline; confirm the five script files are byte-for-byte unchanged
    - _Requirements: 8.1, 8.2, 2.3_
  - [ ]* 12.3 Write the single-account end-to-end smoke test
    - One full orchestrated run against the stubbed AWS surface asserting the four steps run in order and a Deployment_Summary with a Portal URL is produced
    - _Requirements: 1.1, 1.2, 7.1_

- [x] 13. Final checkpoint - full one-command deploy orchestrator complete
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP; core implementation tasks are never optional.
- The orchestrator never edits the five underlying scripts (Req 8.1); it invokes them from the working directory each expects (Req 8.3) and feeds inputs via positional args and constructed stdin.
- Property tests realize the 17 correctness properties as generator-driven `bats` tests running a minimum of 100 generated cases each, tagged `# Feature: one-command-portal-deploy, Property {N}: {title}`, and placed next to the logic they exercise so regressions surface early.
- Unit, pty, and integration tests cover the analyzed non-property criteria (prerequisite messages, context/log-path printing, External-ID reuse, SSO passthrough, URL-unavailable handling, interactive prompting, and standalone-script preservation).
- The orchestrator's steps must remain idempotent and resumable (Req 6.3); the re-run equivalence property (10.4) verifies this. This is a portal/infra orchestrator, not an on-device edge feature, so device/hardware testing is out of scope. All acceptance is verified via `bats` with stubbed AWS — no task requires a live AWS account.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["1.3", "2.1"] },
    { "id": 2, "tasks": ["3.1", "2.2", "2.3"] },
    { "id": 3, "tasks": ["4.1", "3.2"] },
    { "id": 4, "tasks": ["4.2", "4.3"] },
    { "id": 5, "tasks": ["5.1", "4.4"] },
    { "id": 6, "tasks": ["5.2", "5.3"] },
    { "id": 7, "tasks": ["6.1", "5.4", "5.5"] },
    { "id": 8, "tasks": ["8.1", "6.2", "6.3", "6.4", "6.5"] },
    { "id": 9, "tasks": ["8.2"] },
    { "id": 10, "tasks": ["9.1", "8.3", "8.5"] },
    { "id": 11, "tasks": ["10.1", "8.4", "9.2", "9.3", "9.4", "9.5", "9.6"] },
    { "id": 12, "tasks": ["12.1", "10.2", "10.3", "10.4", "10.5"] },
    { "id": 13, "tasks": ["12.2", "12.3"] }
  ]
}
```
