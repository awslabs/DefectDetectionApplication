# Implementation Plan: Ubuntu Pro Build Servers

## Overview

Implementation proceeds pure-logic-first: flavor constants, resolution, and config validation land in `build_domain.py` before any I/O wiring; `build_fleet.py` then gains the parallel Pro AMI tables (standard tables byte-for-byte untouched), the flavor-aware `resolve_ubuntu_ami`, and the launch/list/audit wiring; the CDK stack gets a comment-only update; the CLI launcher's Pro-first-silent-fallback is replaced by an explicit fail-closed `--flavor` option; and the frontend adds the flavor radio and fleet column. Property-based tests (pytest + hypothesis, `test/backend-test/portal_builds/` conventions: moto mocks, fresh-module-import, patched `shared_utils` / `rbac_middleware`, min 100 iterations, tagged `# Feature: ubuntu-pro-build-servers, Property N`) sit alongside the code they validate. The plan ends with the security preservation gate run — expected green with no baseline change since the launcher's IAM heredoc is untouched.

## Tasks

- [x] 1. Implement pure flavor domain logic in build_domain.py
  - [x] 1.1 Add flavor constants, effective-flavor resolver, and read-side defaulting helper
    - Add `UBUNTU_FLAVOR_PRO`, `UBUNTU_FLAVOR_STANDARD`, `UBUNTU_FLAVORS`, and rule identifiers `RULE_UBUNTU_FLAVOR_INVALID`, `RULE_CONFIG_UBUNTU_FLAVOR_INVALID`, `RULE_CONFIG_DEFAULT_FLAVOR_INVALID`
    - Implement `resolve_effective_ubuntu_flavor(requested, configured_default)`: exact case-sensitive `pro`/`standard` accepted; any other non-None requested value errors with `ubuntu_flavor_invalid` naming both supported values; None requested falls back to a valid configured default; None requested with an invalid configured default errors with `config_default_flavor_invalid` identifying the stored value
    - Implement `server_ubuntu_flavor(server)`: stored flavor when in `UBUNTU_FLAVORS`, else `standard`; pure, never writes back
    - _Requirements: 1.1, 1.2, 1.4, 3.3, 6.1, 6.3, 6.4, 6.6_

  - [x] 1.2 Add ubuntu_flavor to DEFAULT_BUILD_CONFIG and validate_build_config
    - Add `'ubuntu_flavor': UBUNTU_FLAVOR_STANDARD` to `DEFAULT_BUILD_CONFIG` with the design's doc comment (makes it an operator-settable, audited `build_config.py` parameter via `KNOWN_PARAMETERS` with no `build_config.py` change)
    - Add the `validate_build_config()` rule: a supplied `ubuntu_flavor` update value must be exactly `pro` or `standard`, else append a `config_ubuntu_flavor_invalid` error naming the supported values (existing `apply_config_update` atomic reject handles retention)
    - _Requirements: 6.4, 6.5_

  - [x]* 1.3 Write property test for configured-default resolution
    - **Property 10: The configured default applies exactly as an explicit selection** — for any configured default d ∈ {pro, standard} and any request value, `resolve_effective_ubuntu_flavor` returns the request's flavor when present, else d; omitting the flavor is indistinguishable from explicitly passing d
    - Pure `build_domain` test, no mocks; min 100 iterations; tag `# Feature: ubuntu-pro-build-servers, Property 10`
    - **Validates: Requirements 6.1, 6.3, 6.4**

  - [x]* 1.4 Write property test for atomic rejection of invalid config flavor
    - **Property 11: Config updates with an invalid flavor are rejected atomically** — for any non-`pro`/`standard` update value and any prior stored config, `apply_config_update` rejects with an error naming the supported values and returns the stored configuration unchanged
    - Pure `build_domain` test, no mocks; min 100 iterations; tag `# Feature: ubuntu-pro-build-servers, Property 11`
    - **Validates: Requirements 6.5**

  - [x]* 1.5 Write property test for invalid stored default failing closed
    - **Property 12: An invalid stored default fails launches closed** — for any stored `ubuntu_flavor` value not exactly `pro` or `standard` and a request omitting the flavor, `resolve_effective_ubuntu_flavor` returns no flavor and a `config_default_flavor_invalid` error identifying the invalid stored default (rejection therefore precedes any EC2 call in the handler)
    - Pure `build_domain` test per the design's resolver-clause scoping; min 100 iterations; tag `# Feature: ubuntu-pro-build-servers, Property 12`
    - **Validates: Requirements 6.6**

  - [x]* 1.6 Write unit tests for config plumbing
    - `ubuntu_flavor` present in `build_config.KNOWN_PARAMETERS` via `DEFAULT_BUILD_CONFIG` (the `test_default_repository_config.py` one-parameter-table pattern)
    - `GET /build-config` returns `standard` when never configured
    - _Requirements: 6.4_

- [x] 2. Add flavor-keyed AMI resolution to build_fleet.py
  - [x] 2.1 Add Ubuntu Pro lookup tables and flavor-keyed dispatch
    - First verify the exact 22.04 pro-server SSM paths against the live Canonical parameter tree (`aws ssm get-parameters-by-path --path /aws/service/canonical/ubuntu/pro-server/22.04 --recursive`) before freezing the constants
    - Add `UBUNTU_PRO_2204_SSM_PARAMETER`, `UBUNTU_PRO_2404_SSM_PARAMETER`, `UBUNTU_PRO_2204_NAME_FILTER`, `UBUNTU_PRO_2404_NAME_FILTER` (Canonical owner 099720109477, `ubuntu-pro-server` patterns, verified volume-type segments)
    - Add `UBUNTU_SSM_PARAMETER_BY_FLAVOR` and `UBUNTU_NAME_FILTER_BY_FLAVOR` dispatch tables; the standard branch references the EXISTING constants unchanged, and both flavors carry identical (release, arch) key sets
    - Do not edit the existing standard SSM parameter or name-filter constants in any way
    - _Requirements: 2.1, 2.4, 2.5_

  - [x] 2.2 Extend resolve_ubuntu_ami with a flavor parameter
    - Signature `resolve_ubuntu_ami(arch, ubuntu_version=DEFAULT_UBUNTU_VERSION, ubuntu_flavor=build_domain.UBUNTU_FLAVOR_STANDARD)` so every existing caller (including `build_dispatcher.py`) is behavior-preserved
    - Resolution shape unchanged: SSM `get_parameter` primary keyed by flavor; a non-empty SSM value returns immediately with zero DescribeImages calls; on `ClientError` or empty value, DescribeImages fallback (Canonical owner, flavor's name filter, `state=available`, newest `CreationDate` wins); `RuntimeError` naming flavor, release, and architecture when no table mapping exists or the fallback matches zero images
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

  - [x]* 2.3 Write unit tests for table preservation, parity, and IAM prefix coverage
    - Standard-table preservation: the standard SSM parameter and name-filter constants equal frozen pre-feature literals
    - Table parity: the Pro tables' (release, arch) key sets equal the standard tables'
    - IAM prefix coverage: every SSM path in both flavor tables starts with `/aws/service/canonical/` (the `grantAmiParameterRead` ARN prefix), so a future grant narrowing fails a test rather than production resolution
    - _Requirements: 2.4, 2.5, 7.1_

  - [x]* 2.4 Write property test for the Pro SSM short-circuit
    - **Property 4: A successful Pro SSM read short-circuits** — for any supported release/architecture and any non-empty AMI id from the Pro SSM read, `resolve_ubuntu_ami` resolves to exactly that id with zero DescribeImages calls
    - Resolver test with stubbed SSM/EC2; min 100 iterations; tag `# Feature: ubuntu-pro-build-servers, Property 4`
    - **Validates: Requirements 2.6**

  - [x]* 2.5 Write property test for the Pro DescribeImages fallback
    - **Property 5: The Pro fallback selects the newest available image** — for any supported combination where the Pro SSM read fails or is empty, and any non-empty candidate set with distinct creation timestamps, the fallback queries owner 099720109477 with the Pro name pattern and `state=available` and resolves the most recently created image
    - Resolver test with stubbed SSM/EC2; min 100 iterations; tag `# Feature: ubuntu-pro-build-servers, Property 5`
    - **Validates: Requirements 2.2**

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Wire flavor into the launch handler and fleet responses
  - [x] 4.1 Wire launch_build_server for flavor resolution, rejection, persistence, and audit
    - Read `body.get('ubuntu_flavor')` alongside the existing launch fields; leave the existing name/architecture/release validation unchanged (it already enforces the supported combinations 22.04 arm64+x86_64, 24.04 arm64 for both flavors)
    - Call `build_domain.resolve_effective_ubuntu_flavor(body.get('ubuntu_flavor'), config['ubuntu_flavor'])`; errors join `validation_errors` into the existing `400 LAUNCH_REQUEST_INVALID` envelope before any EC2 call and before any BuildServers write
    - Add the failure audit call to the validation-rejection branch carrying `ubuntu_flavor` exactly as submitted in the request (raw body value)
    - Pass the effective flavor to `resolve_ubuntu_ami`; resolution or RunInstances failure keeps the existing `502 LAUNCH_FAILED` path with `ubuntu_flavor: effective_flavor` added to the audit details
    - Persist `'ubuntu_flavor': effective_flavor` on the server record before the 201 response; add the effective flavor to the success audit details
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.3, 3.1, 3.4, 3.5, 6.1, 6.3, 6.6_

  - [x] 4.2 Add present_server read-side defaulting to fleet responses
    - Implement `present_server(server)` returning the record with `ubuntu_flavor` filled via `build_domain.server_ubuntu_flavor` — legacy records report `standard`, the stored item is never modified
    - Apply in `list_build_servers` (every listed server regardless of lifecycle state) and in `execute_fleet_action`'s 200 response
    - _Requirements: 3.2, 3.3_

  - [x]* 4.3 Write property test for flavor-matched AMI lookup
    - **Property 1: Flavor selects the matching AMI lookup** — for any valid launch (flavor ∈ {pro, standard}, supported release/arch), the resolver reads exactly the requested flavor's SSM path for that release/arch and the launched instance uses the resolved AMI id
    - Handler test via `build_fleet.launch_build_server` with moto + patched clients; min 100 iterations; tag `# Feature: ubuntu-pro-build-servers, Property 1`
    - **Validates: Requirements 1.1, 1.2, 2.1**

  - [x]* 4.4 Write property test for flavorless pre-feature preservation
    - **Property 2: Flavorless launches preserve pre-feature standard resolution** — for any valid launch omitting `ubuntu_flavor` with no default stored in Build_Config, resolution reads a standard SSM path byte-identical to the pre-feature path (and the fallback, if reached, uses the byte-identical pre-feature name filter)
    - Handler test with moto + patched clients; min 100 iterations; tag `# Feature: ubuntu-pro-build-servers, Property 2`
    - **Validates: Requirements 1.3, 2.4, 6.4**

  - [x]* 4.5 Write property test for side-effect-free rejection
    - **Property 3: Invalid launch requests are rejected with no side effects** — for any request with a non-`pro`/`standard` flavor value (empty, differently cased, non-string) or an unsupported flavor/release/arch combination: 400 naming the supported values (or identifying the combination), zero EC2 API calls, zero BuildServers writes
    - Handler test with moto + patched clients; min 100 iterations; tag `# Feature: ubuntu-pro-build-servers, Property 3`
    - **Validates: Requirements 1.4, 1.5, 1.6**

  - [x]* 4.6 Write property test for fail-closed unresolvable Pro AMI
    - **Property 6: An unresolvable Pro AMI fails the launch closed** — for any supported combination where both the Pro SSM read and the fallback fail, the launch fails with an error identifying flavor/release/arch, records an Audit_Log failure entry, and invokes zero RunInstances calls
    - Handler test with moto + patched clients; min 100 iterations; tag `# Feature: ubuntu-pro-build-servers, Property 6`
    - **Validates: Requirements 2.3**

  - [x]* 4.7 Write property test for record and fleet-list round trip
    - **Property 7: The effective flavor round-trips through the record and the fleet list** — for any successful launch, the record persisted before the response carries the effective flavor, and a subsequent fleet list reports exactly the stored flavor for that server in any lifecycle state
    - Handler test via `launch_build_server` + `list_build_servers` with moto; min 100 iterations; tag `# Feature: ubuntu-pro-build-servers, Property 7`
    - **Validates: Requirements 3.1, 3.2**

  - [x]* 4.8 Write property test for legacy record defaulting without write-back
    - **Property 8: Legacy records read as standard without write-back** — for any BuildServers record with no `ubuntu_flavor` field, every response including it reports `standard`, and the stored record afterwards still carries no `ubuntu_flavor` attribute
    - Handler test with moto; min 100 iterations; tag `# Feature: ubuntu-pro-build-servers, Property 8`
    - **Validates: Requirements 3.3**

  - [x]* 4.9 Write property test for faithful audit flavor
    - **Property 9: Audit entries carry the flavor faithfully** — for any launch outcome: post-determination entries (success or resolution/RunInstances failure) carry exactly the effective flavor; pre-determination rejections carry the `ubuntu_flavor` value exactly as submitted
    - Handler test with moto + patched clients; min 100 iterations; tag `# Feature: ubuntu-pro-build-servers, Property 9`
    - **Validates: Requirements 3.4, 3.5**

- [x] 5. Update infrastructure stack documentation
  - [x] 5.1 Update the grantAmiParameterRead comment in build-fleet-stack.ts
    - Comment-only change documenting that the `parameter/aws/service/canonical/*` ARN covers both the `ubuntu/server` and `ubuntu/pro-server` subtrees and that `grantEc2Describe` covers the DescribeImages fallback; no functional grant change
    - _Requirements: 7.1, 7.2, 7.5_

- [x] 6. Add explicit --flavor to the CLI launcher
  - [x] 6.1 Implement --flavor with fail-closed single-flavor AMI resolution in launch-arm64-build-server.sh
    - Add the `--flavor FLAVOR` option (`UBUNTU_FLAVOR="standard"` default) and its help text
    - Validate immediately after argument parsing, before the IAM setup section: any value other than exactly `pro` or `standard` prints an error naming the two supported values and exits nonzero before any AWS API call
    - Replace the Pro-first-silent-fallback AMI resolution with a single flavor-selected DescribeImages query (`ubuntu-pro-server/images/${UBUNTU_SSD_PATH}/ubuntu-${UBUNTU_CODENAME}-${UBUNTU_VERSION}-arm64-pro-server-*` for pro, existing standard pattern otherwise); on no match, exit nonzero naming the selected flavor and release without querying the other flavor and without launching; the existing `${UBUNTU_SSD_PATH}` split covers 18.04/20.04/22.04 (hvm-ssd) and 24.04 (hvm-ssd-gp3)
    - `--ami-id` continues to skip the resolution block entirely
    - Add `Ubuntu Flavor: ${UBUNTU_FLAVOR}` to the configuration summary alongside the AMI id line, printed before the dry-run exit
    - Do NOT touch the `DDABuildPolicy` IAM heredoc
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8_

  - [x]* 6.2 Write CLI launcher tests with a PATH-shimmed aws executable
    - Test module drives the script with a shim `aws` that records every invocation and returns scripted output
    - Assert: `--flavor pro` issues exactly one DescribeImages with the pro pattern and none with standard; `--flavor standard` and no flavor mirror that; unresolvable AMI exits nonzero naming flavor and release with no cross-flavor query and no run-instances; invalid flavor exits nonzero naming `pro`/`standard` with the shim never invoked; `--ami-id` skips resolution; `--dry-run` summary contains flavor and AMI id; the Pro pattern is correct for 18.04/20.04/22.04/24.04 including the `hvm-ssd-gp3` segment for 24.04
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8_

- [x] 7. Add flavor selection and display to the frontend
  - [x] 7.1 Add flavor types to api.ts
    - Add `BuildServerUbuntuFlavor = 'pro' | 'standard'`; add `ubuntu_flavor?` to `BuildServer`, to the `launchBuildServer` body type, and to `BuildInfrastructureConfig`
    - _Requirements: 4.3, 4.4, 6.2_

  - [x] 7.2 Add the Ubuntu flavor radio to LaunchServerModal in FleetPage.tsx
    - New state `ubuntuFlavor` initialized to `'standard'`; on modal mount, `apiService.getBuildConfig()` flips the selection to `pro` only when `config.ubuntu_flavor === 'pro'`; any retrieval failure or invalid value leaves `standard` selected; the fetch never blocks the modal
    - RadioGroup labeled "Ubuntu flavor" with exactly two items — `standard` ("Regular Ubuntu server") and `pro` ("Ubuntu Pro — extended security maintenance") — with a non-null initial value so one is always selected
    - `submit()` adds `ubuntu_flavor: ubuntuFlavor` to the existing launch body; the existing `serverError` path retains all field state including the flavor selection
    - _Requirements: 4.1, 4.2, 4.3, 4.5, 4.6, 6.2, 6.7_

  - [x] 7.3 Add the Ubuntu flavor column to the fleet table in FleetPage.tsx
    - New "Ubuntu flavor" column rendering each server's `ubuntu_flavor` from the fleet list response, displayed as "Pro" / "Standard" (backend fills `standard` for legacy records)
    - _Requirements: 4.4_

  - [x]* 7.4 Write frontend tests for flavor selection and display
    - Extend `FleetPage.test.tsx` (React Testing Library, mocked `apiService`): two-value radio with one always selected; preselection from `getBuildConfig` (`pro` default, absent default, fetch failure → `standard`); `ubuntu_flavor` in the submitted body; flavor column rendering; error-path state retention
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 6.2, 6.7_

- [x] 8. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Verify the security preservation gate
  - [x] 9.1 Run the full security preservation suite and reconcile baselines
    - Run the full `test/backend-test/security/preservation/` suite; expected outcome is green with no baseline change since the `DDABuildPolicy` heredoc is untouched
    - If any tracked content unexpectedly shifted, update the matching baseline under `test/backend-test/security/baselines/` (e.g. `iam_baseline_heredoc_launch-arm64-build-server_DDABuildPolicy.json`) in the same commit per the gate's README
    - Never modify, weaken, skip, or remove gate test logic; baseline value updates are the only permitted gate-related change
    - _Requirements: 7.3, 7.4, 7.6_

- [x] 10. Add default flavor administration to the Build Infrastructure settings page
  - [x] 10.1 Implement the Default Ubuntu flavor field in BuildInfrastructureSettings.tsx
    - Add `ubuntu_flavor: 'pro' | 'standard'` (default `'standard'`) to `BuildConfigFormState` and `EMPTY_FORM` — the `EMPTY_FORM` default also covers the load-failure fallback: a failed `GET /build-config` leaves the form at `EMPTY_FORM` with the existing load-error notice and `standard` selected (Req 8.6)
    - Map `config.ubuntu_flavor` in `toFormState()`, coercing absent or invalid stored values to `'standard'` (Req 8.1)
    - Render a Cloudscape RadioGroup inside a FormField labeled "Default Ubuntu flavor" with constraint text and `errorText={fieldErrors.ubuntu_flavor}`, items Standard Ubuntu / Ubuntu Pro, with one always selected (Req 8.2)
    - Add `ubuntu_flavor: form.ubuntu_flavor` to the `handleSave` update object — NOT via `textValue()`, since the value is never null (Req 8.3)
    - `mapConfigErrors` routes per-parameter errors onto the field automatically once the key is in `EMPTY_FORM` (Req 8.4, 8.7); `applyConfig(response.config)` already reflects the stored value after a successful save (Req 8.5)
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7_

  - [x]* 10.2 Write frontend tests for default flavor administration
    - **Property 13: The settings page round-trips the default flavor faithfully**
    - Extend `BuildInfrastructureSettings.test.tsx` (vitest + RTL, mocked `apiService`, following the file's existing conventions) covering Req 8.1–8.7: displayed selection reflects the loaded value (`pro`, `standard`, absent → `standard`, invalid → `standard`, load failure → `standard` with the load-error notice); the RadioGroup offers exactly the two labeled values with one always selected; every `updateBuildConfig` body carries `ubuntu_flavor` as exactly the selected string, never null, including an unchanged save; a CONFIG_INVALID rejection naming `ubuntu_flavor` renders on the field with all form state retained; a rejection naming other parameters or a plain failure leaves the selection unchanged; a successful save reflects the PUT response's stored value
    - Tag: `# Feature: ubuntu-pro-build-servers, Property 13`
    - **Validates: Requirements 8.1-8.7**

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability; each property test cites its design property number
- Standard AMI lookup constants are never edited (Requirement 2.4 byte-for-byte preservation); the Pro tables are strictly additive
- Task 2.1 begins with the design-mandated live verification of the 22.04 pro-server SSM paths before the constants are frozen
- Backend tests follow `test/backend-test/portal_builds/` conventions: pytest + hypothesis (min 100 iterations, `# Feature: ubuntu-pro-build-servers, Property N` tags), moto mocks, fresh-module-import, patched `shared_utils` / `rbac_middleware`
- The plan ends with the preservation gate run (task 9.1); no baseline change is expected because the launcher's IAM heredoc is untouched
- The post-deploy integration check of Requirement 7.2 in a live environment is outside this coding plan; task 2.3's IAM prefix test pins the grant coverage in code

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "5.1", "6.1", "7.1"] },
    { "id": 1, "tasks": ["1.2", "2.1", "6.2", "7.2"] },
    { "id": 2, "tasks": ["1.3", "2.2", "7.3"] },
    { "id": 3, "tasks": ["1.4", "2.3", "4.1", "7.4"] },
    { "id": 4, "tasks": ["1.5", "2.4", "4.2", "4.3"] },
    { "id": 5, "tasks": ["1.6", "2.5", "4.4"] },
    { "id": 6, "tasks": ["4.5"] },
    { "id": 7, "tasks": ["4.6"] },
    { "id": 8, "tasks": ["4.7"] },
    { "id": 9, "tasks": ["4.8"] },
    { "id": 10, "tasks": ["4.9"] },
    { "id": 11, "tasks": ["9.1"] },
    { "id": 12, "tasks": ["10.1", "10.2"] }
  ]
}
```
