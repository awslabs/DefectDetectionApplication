# Requirements Document

## Introduction

For a cross-account use case, Station Quick Setup issues the station's Provisioning_Credentials by assuming the use-case account's `DDAPortalAccessRole` (with the use case's external id), narrowed by the per-device session policy. `DDAPortalAccessRole` (`edge-cv-portal/infrastructure/lib/usecase-account-stack.ts`) is the portal's general cross-account role. It does not grant the actions the Greengrass installer needs to provision a device:

- `iot:CreateThing`, `iot:CreateKeysAndCertificate`, `iot:AttachPolicy`, `iot:AttachThingPrincipal`;
- `iot:GetPolicy` and `iot:CreatePolicy` (IoT), `iot:DescribeRoleAlias` and `iot:CreateRoleAlias`;
- `iam:GetRole` and `iam:GetPolicy`.

The effective permissions are the intersection, so a cross-account Quick Setup is expected to fail at the installer's provisioning step. This comes from reading the stack; no cross-account Quick Setup has been run. The token exchange role preparation of #110 is limited there too: it can only write the role's inline policies (`DeviceRoleInlinePolicyGrants`). It cannot create the role, its role alias or its access policy, so a fresh use-case account cannot be prepared.

Same-account use cases do not have this problem, because they use the dedicated `DDAStationProvisioningRole` in the portal account. This feature gives every use-case account its own `DDAStationProvisioningRole`, so cross-account Quick Setup provisions devices the same way.

## Glossary

- **Use_Case_Account**: the AWS account a cross-account use case names; it runs the UseCase account stack.
- **Portal_Access_Role**: `DDAPortalAccessRole` in the Use_Case_Account, the portal's general cross-account role.
- **Station_Provisioning_Role**: a role named `DDAStationProvisioningRole` that holds the Quick Setup provisioning ceiling. The portal account has one for same-account use cases; this feature adds one per Use_Case_Account.
- **Quick_Setup_Handler_Role**: the portal account role of the `quick_setup` Lambda.
- **Ceiling**: the actions a Station_Provisioning_Role allows. Every session from it is narrowed further by a session policy: the station's or the Lambda's Preparation_Session.

## Requirements

### Requirement 1: A provisioning role in every use-case account

**User Story:** As a portal administrator with a multi-account install, I want Quick Setup to provision devices in a use-case account, so that I do not need a manual installation there.

#### Acceptance Criteria

1. THE UseCase account stack SHALL create a Station_Provisioning_Role with the same Ceiling as the portal account's Station_Provisioning_Role, scoped to the Use_Case_Account.
2. THE Station_Provisioning_Role SHALL trust only the portal account's Quick_Setup_Handler_Role, with the use case's external id (`sts:ExternalId`). No other principal SHALL be able to assume it.
3. THE Quick_Setup_Handler_Role SHALL be allowed `sts:AssumeRole` on `DDAStationProvisioningRole` in each trusted use-case account only (`trustedUseCaseAccountIds`).
4. WHERE a permissions boundary or tag conditions apply to the Use_Case_Account's deploy (the least-privilege follow-ups), THE new role SHALL comply with them.

### Requirement 2: Quick Setup uses it for cross-account use cases

**User Story:** As a station operator, I want the same setup command to work for every use case.

#### Acceptance Criteria

1. WHEN a station exchanges a token for a cross-account use case, THE Quick_Setup_Endpoint SHALL assume the Use_Case_Account's Station_Provisioning_Role, not the Portal_Access_Role. This applies to both the station's credentials and the Preparation_Session.
2. THE token exchange role preparation SHALL then do the full set of steps it does for a same-account use case: role, role alias, access policy and inline policies. The IoT policy preparation (the `quick-setup-iot-thing-policy` spec) SHALL do the same when it exists.
3. IF the Station_Provisioning_Role does not exist or cannot be assumed (for example, a Use_Case_Account whose stack predates this feature), THEN THE Quick_Setup_Endpoint SHALL return an error that names the remedy (redeploy the UseCase account stack) and SHALL leave the token unconsumed.
4. THE station's session policy SHALL be the same as for a same-account use case.

### Requirement 3: The general cross-account role does not grow

**User Story:** As a security reviewer, I want the portal's general cross-account role to stay as narrow as it is.

#### Acceptance Criteria

1. THE Portal_Access_Role SHALL gain no IoT provisioning or IAM write actions for Quick Setup.
2. THE `DeviceRoleInlinePolicyGrants` statement SHALL remain the Portal_Access_Role's only IAM write on the token exchange role, for the portal's own device inline policies.

### Requirement 4: Rollout

#### Acceptance Criteria

1. THE README and ADMIN_GUIDE SHALL say that each Use_Case_Account must redeploy its UseCase account stack before Quick Setup works there.
2. THE UseCase installer and execution policies (`edge-cv-portal/iam/usecase-*.json`) SHALL allow creating and updating the new role, scoped to its name.
3. THE `iam/setup-account.sh usecase` dry run SHALL show the policy changes.
4. A live cross-account Quick Setup SHALL be run before release: portal account plus one use-case account, a fresh station, CloudTrail checked for denials.
