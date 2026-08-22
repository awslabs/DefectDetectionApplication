# Requirements Document

## Introduction

Dedicated build servers are EC2 instances provisioned by the edge-cv-portal build fleet (`POST /build-servers` in `edge-cv-portal/backend/functions/build_fleet.py`) and, for manual provisioning, by the CLI launcher `edge-cv-portal/launch-arm64-build-server.sh`. Today the portal backend resolves only standard (non-Pro) Canonical Ubuntu AMIs, while the CLI launcher tries Ubuntu Pro AMIs first and silently falls back to standard Ubuntu — so neither path gives the operator a deterministic, auditable choice.

Organizational security requirements mandate that build servers can run Ubuntu Pro (extended security maintenance) to remain in compliance. This feature adds an explicit Ubuntu flavor selection — Ubuntu Pro or standard Ubuntu — at provisioning time in both the portal launch path and the CLI launcher, with AMI resolution extended to Canonical's Ubuntu Pro SSM parameters and AMI name patterns. Existing behavior is preserved as the default in the portal path: a launch request that does not select a flavor provisions standard Ubuntu exactly as today. In the CLI launcher, the current implicit Pro-first-with-silent-fallback behavior is replaced by an explicit flag, because a silent fallback to standard Ubuntu would defeat the compliance guarantee. An optional organization-wide default flavor lets administrators mandate Pro without requiring every operator to select it on each launch.

## Glossary

- **Fleet_Manager**: The portal build fleet Lambda handler (`build_fleet.py`) that serves `POST /build-servers` and launches Dedicated_Build_Servers via EC2 RunInstances.
- **Dedicated_Build_Server**: An EC2 instance provisioned by the Fleet_Manager or the CLI_Launcher and registered in the BuildServers table for running component builds.
- **CLI_Launcher**: The manual provisioning script `edge-cv-portal/launch-arm64-build-server.sh`.
- **Fleet_Page**: The portal admin frontend page (`FleetPage.tsx`) containing the launch-server dialog.
- **AMI_Resolver**: The AMI resolution logic in the Fleet_Manager (`resolve_ubuntu_ami`) that maps an Ubuntu release, CPU architecture, and Ubuntu_Flavor to a Canonical AMI id via SSM public parameters with a DescribeImages fallback.
- **Ubuntu_Flavor**: The Ubuntu offering selected for a Dedicated_Build_Server: `pro` (Ubuntu Pro, extended security maintenance) or `standard` (regular Ubuntu server).
- **Pro_AMI**: An Ubuntu Pro AMI published by Canonical (owner id 099720109477) under Ubuntu Pro SSM parameter paths and `ubuntu-pro-server` AMI name patterns.
- **Standard_AMI**: A regular Ubuntu server AMI published by Canonical under the `ubuntu/images` name patterns and `/aws/service/canonical/ubuntu/server/...` SSM parameter paths used today.
- **Build_Fleet_Stack**: The CDK stack `edge-cv-portal/infrastructure/lib/build-fleet-stack.ts` that grants the fleet and dispatcher Lambda roles read access to Canonical AMI SSM parameters.
- **Build_Config**: The `build_infrastructure_config` item in the PortalSettings table (managed by `build_config.py`) holding operator-configurable build infrastructure defaults.
- **Audit_Log**: The portal audit trail where every fleet action outcome is recorded.
- **Security_Preservation_Gate**: The backend build gate (`test/backend-test/security/preservation/`) that pins hashes of security-relevant files, including the CLI_Launcher's embedded IAM policy baseline (`test/backend-test/security/baselines/iam_baseline_heredoc_launch-arm64-build-server_DDABuildPolicy.json`).

## Requirements

### Requirement 1: Ubuntu Flavor Selection at Portal Launch

**User Story:** As a portal administrator, I want to choose between Ubuntu Pro and standard Ubuntu when launching a dedicated build server, so that I can provision compliant build servers when my organization's security requirements mandate Ubuntu Pro.

#### Acceptance Criteria

1. WHEN a launch request includes the Ubuntu_Flavor field with the exact case-sensitive value `pro`, THE Fleet_Manager SHALL provision the Dedicated_Build_Server from a Pro_AMI matching the requested CPU architecture and the requested Ubuntu release (or the 22.04 default Ubuntu release when the request omits the release).
2. WHEN a launch request includes the Ubuntu_Flavor field with the exact case-sensitive value `standard`, THE Fleet_Manager SHALL provision the Dedicated_Build_Server from a Standard_AMI matching the requested CPU architecture and the requested Ubuntu release (or the 22.04 default Ubuntu release when the request omits the release).
3. WHEN a launch request omits the Ubuntu_Flavor field and no default Ubuntu_Flavor is configured in Build_Config, THE Fleet_Manager SHALL provision the Dedicated_Build_Server from a Standard_AMI resolved with the same Ubuntu release and CPU architecture inputs that an identical launch request resolved to before this feature.
4. IF a launch request includes an Ubuntu_Flavor value that is not the exact case-sensitive string `pro` or `standard` (including empty and differently cased values), THEN THE Fleet_Manager SHALL reject the request with a 400 validation error that names `pro` and `standard` as the supported Ubuntu_Flavor values.
5. IF the requested combination of Ubuntu_Flavor, Ubuntu release, and CPU architecture is outside the supported combinations (22.04 on arm64 and x86_64, 24.04 on arm64), THEN THE Fleet_Manager SHALL reject the request with a 400 validation error identifying the unsupported Ubuntu_Flavor, Ubuntu release, and CPU architecture combination.
6. IF the Fleet_Manager rejects a launch request with a 400 validation error, THEN THE Fleet_Manager SHALL make no EC2 API call and SHALL create no BuildServers record for that request.

### Requirement 2: Ubuntu Pro AMI Resolution

**User Story:** As a portal administrator, I want the system to resolve the latest Canonical-published Ubuntu Pro AMI for my selected release and architecture, so that Pro build servers launch from current, vendor-maintained images.

#### Acceptance Criteria

1. WHEN resolving a Pro_AMI for a supported Ubuntu release and CPU architecture, THE AMI_Resolver SHALL read the Canonical-maintained Ubuntu Pro SSM public parameter for that release and architecture as the primary lookup.
2. IF the Ubuntu Pro SSM parameter lookup fails — the parameter read returns an error, or returns a missing or empty AMI id — THEN THE AMI_Resolver SHALL fall back to an EC2 DescribeImages query filtered to Canonical owner id 099720109477, the Ubuntu Pro AMI name pattern for that release and architecture, and images in the `available` state, and SHALL select the image with the most recent creation timestamp from the results.
3. IF both the Ubuntu Pro SSM parameter lookup and the DescribeImages fallback fail to resolve a Pro_AMI — the fallback query returns an error or matches zero images — THEN THE Fleet_Manager SHALL fail the launch with an error identifying the requested Ubuntu_Flavor, Ubuntu release, and CPU architecture, SHALL record the failure in the Audit_Log, and SHALL NOT invoke EC2 RunInstances for the request.
4. WHEN resolving a Standard_AMI, THE AMI_Resolver SHALL use the same SSM parameter paths and DescribeImages name filters used before this feature, byte-for-byte unchanged.
5. THE AMI_Resolver SHALL support the Pro_AMI lookup for every Ubuntu release and CPU architecture combination the Fleet_Manager supports for Standard_AMI launches (22.04 on arm64 and x86_64, 24.04 on arm64).
6. WHEN the Ubuntu Pro SSM parameter read returns a non-empty AMI id, THE AMI_Resolver SHALL resolve the Pro_AMI to that AMI id without issuing a DescribeImages query.

### Requirement 3: Flavor Recorded and Visible on the Fleet

**User Story:** As a portal administrator, I want each build server's Ubuntu flavor recorded and visible, so that I can demonstrate to security reviewers which servers run Ubuntu Pro.

#### Acceptance Criteria

1. WHEN the Fleet_Manager successfully launches a Dedicated_Build_Server, THE Fleet_Manager SHALL persist the effective Ubuntu_Flavor (exactly `pro` or `standard`) on the server's BuildServers record before returning the launch response.
2. WHEN the fleet list is requested, THE Fleet_Manager SHALL include each server's Ubuntu_Flavor in the response for every listed server regardless of lifecycle state, exactly matching the value recorded on the server's BuildServers record.
3. IF a BuildServers record carries no Ubuntu_Flavor field, THEN THE Fleet_Manager SHALL report that server's Ubuntu_Flavor as `standard` in every response that includes that server, without modifying the stored BuildServers record.
4. WHEN a launch succeeds, or fails after the Fleet_Manager has determined the effective Ubuntu_Flavor (the flavor determined after applying the configured default), THE Fleet_Manager SHALL include the effective Ubuntu_Flavor in the corresponding Audit_Log entry.
5. IF a launch request is rejected before the Fleet_Manager determines an effective Ubuntu_Flavor (for example, rejection of an unsupported flavor value), THEN THE Fleet_Manager SHALL include the Ubuntu_Flavor value exactly as submitted in the request in the corresponding Audit_Log entry.

### Requirement 4: Frontend Flavor Selection

**User Story:** As a portal administrator, I want the launch dialog to offer the Ubuntu Pro choice, so that I can select the compliant flavor without crafting API requests by hand.

#### Acceptance Criteria

1. WHEN the launch dialog is opened, THE Fleet_Page SHALL present an Ubuntu_Flavor selection offering exactly the two values `pro` and `standard`, with exactly one of the two values selected at all times while the dialog is open.
2. WHEN the launch dialog is opened and no organization default Ubuntu_Flavor is configured in Build_Config, THE Fleet_Page SHALL preselect the `standard` Ubuntu_Flavor.
3. WHEN the administrator submits the launch dialog, THE Fleet_Page SHALL include the selected Ubuntu_Flavor in the same `POST /build-servers` request body that carries the existing launch fields.
4. WHEN the fleet list is displayed, THE Fleet_Page SHALL display, for each server, the Ubuntu_Flavor value returned for that server in the fleet list response.
5. IF the launch request is rejected by the Fleet_Manager, THEN THE Fleet_Page SHALL display the returned error in the launch dialog and SHALL retain the administrator's Ubuntu_Flavor selection and other entered values for correction and resubmission.
6. IF the organization default Ubuntu_Flavor cannot be retrieved when the launch dialog is opened, THEN THE Fleet_Page SHALL preselect the `standard` Ubuntu_Flavor.

### Requirement 5: CLI Launcher Parity

**User Story:** As an operator provisioning build servers manually, I want the CLI launcher to take an explicit Ubuntu Pro or standard Ubuntu selection, so that manual launches are deterministic and meet the same compliance requirements as portal launches.

#### Acceptance Criteria

1. THE CLI_Launcher SHALL accept a command-line option selecting the Ubuntu_Flavor, accepting exactly the values `pro` and `standard`.
2. WHEN the CLI_Launcher is invoked with the `pro` Ubuntu_Flavor and no explicit AMI id option is supplied, THE CLI_Launcher SHALL resolve the most recently created available Pro_AMI for the requested Ubuntu release, without performing any Standard_AMI lookup.
3. WHEN the CLI_Launcher is invoked with the `standard` Ubuntu_Flavor or without a flavor option, and no explicit AMI id option is supplied, THE CLI_Launcher SHALL resolve the most recently created available Standard_AMI for the requested Ubuntu release, without performing any Pro_AMI lookup.
4. IF the CLI_Launcher cannot resolve an AMI for the selected Ubuntu_Flavor and requested Ubuntu release, THEN THE CLI_Launcher SHALL exit with a nonzero status and an error message identifying the selected Ubuntu_Flavor and Ubuntu release, without querying for an AMI of the other Ubuntu_Flavor and without launching any instance.
5. IF the CLI_Launcher is invoked with a flavor value other than `pro` or `standard`, THEN THE CLI_Launcher SHALL exit with a nonzero status and an error message naming the supported Ubuntu_Flavor values, before making any AWS API call.
6. WHEN the CLI_Launcher prints its launch configuration summary, including under a dry-run invocation, THE CLI_Launcher SHALL include the selected Ubuntu_Flavor and the resolved AMI id.
7. WHEN the CLI_Launcher is invoked with an explicit AMI id option, THE CLI_Launcher SHALL launch from the supplied AMI id and skip flavor-based AMI resolution.
8. THE CLI_Launcher SHALL support Pro_AMI resolution for every Ubuntu release it supports for Standard_AMI resolution (18.04, 20.04, 22.04, and 24.04).

### Requirement 6: Organization Default Flavor

**User Story:** As a portal administrator, I want to configure an organization-wide default Ubuntu flavor, so that our compliance mandate for Ubuntu Pro applies to every launch without relying on each operator to remember the selection.

#### Acceptance Criteria

1. WHERE a default Ubuntu_Flavor is configured in Build_Config, WHEN a launch request omits the Ubuntu_Flavor, THE Fleet_Manager SHALL apply the configured default as the effective Ubuntu_Flavor and SHALL provision, record, and audit the launch exactly as if the request had explicitly included that value.
2. WHERE a default Ubuntu_Flavor is configured in Build_Config, WHEN the administrator opens the launch dialog, THE Fleet_Page SHALL retrieve the configured default and preselect it as the Ubuntu_Flavor selection.
3. WHEN a launch request explicitly includes an Ubuntu_Flavor, THE Fleet_Manager SHALL use the requested Ubuntu_Flavor regardless of the configured default.
4. WHEN no default Ubuntu_Flavor is stored in Build_Config, THE Fleet_Manager SHALL treat the default as `standard`.
5. IF an update to Build_Config sets the default Ubuntu_Flavor to a value other than `pro` or `standard`, THEN THE Build_Config SHALL reject the update with a validation error naming the supported values and SHALL retain the previously stored configuration unchanged.
6. IF the default Ubuntu_Flavor stored in Build_Config is a value other than `pro` or `standard`, THEN THE Fleet_Manager SHALL reject launch requests that omit the Ubuntu_Flavor with a validation error identifying the invalid stored default, before any EC2 API call is made.
7. IF the Fleet_Page cannot retrieve the configured default Ubuntu_Flavor when the launch dialog is opened, THEN THE Fleet_Page SHALL preselect `standard` in the launch dialog.

### Requirement 7: Infrastructure Permissions and Security Gate Compliance

**User Story:** As a platform maintainer, I want the deployed IAM grants to cover Ubuntu Pro AMI resolution and the security preservation baselines to stay green, so that the feature works in deployment and the build gate does not fail on intended changes.

#### Acceptance Criteria

1. THE Build_Fleet_Stack SHALL grant the fleet and dispatcher Lambda roles ssm:GetParameter read access covering every Canonical Ubuntu Pro SSM public parameter path the AMI_Resolver reads, for each supported Ubuntu release and CPU architecture combination (22.04 on arm64 and x86_64, 24.04 on arm64).
2. WHEN the Fleet_Manager resolves a Pro_AMI in a deployed environment, THE AMI_Resolver SHALL return the resolved Pro_AMI id (via the SSM parameter read, or via the DescribeImages fallback when the SSM read fails) without any authorization error from the SSM or EC2 APIs, using only the permissions granted by the Build_Fleet_Stack.
3. IF this feature changes the CLI_Launcher's embedded IAM policy document, THEN THE implementation SHALL update the matching Security_Preservation_Gate baseline (`iam_baseline_heredoc_launch-arm64-build-server_DDABuildPolicy.json`) in the same commit as the policy change, such that the full Security_Preservation_Gate test suite passes with zero failures.
4. IF this feature changes any other file tracked by the Security_Preservation_Gate, THEN THE implementation SHALL update the corresponding baseline under `test/backend-test/security/baselines/` in the same commit as the tracked-file change, such that the full Security_Preservation_Gate test suite passes with zero failures.
5. THE Build_Fleet_Stack SHALL grant the fleet and dispatcher Lambda roles the ec2:DescribeImages permission required by the AMI_Resolver's DescribeImages fallback for Pro_AMI resolution.
6. THE implementation SHALL NOT modify, weaken, skip, or remove Security_Preservation_Gate test logic to make the gate pass; updating baseline values SHALL be the only permitted gate-related change.
