# Requirements Document

## Introduction

Station Quick Setup gives a station short-lived Provisioning_Credentials so that the Greengrass installer (`--provision true`) and `station_install/setup_station.sh` can provision the device. Since #110 the portal prepares the Greengrass token exchange role itself, and the station's credentials carry no IAM write action. The IoT side still has the same problem. The station's session policy (`edge-cv-portal/backend/functions/session_policy.py`), intersected with `DDAStationProvisioningRole`, allows:

- `iot:CreatePolicy` and `iot:GetPolicy` on `policy/GreengrassV2IoTThingPolicy` and `policy/GreengrassTESCertificatePolicy*`;
- `iot:ListPolicyVersions`, `iot:CreatePolicyVersion` and `iot:DeletePolicyVersion` on `policy/GreengrassV2IoTThingPolicy`;
- `iot:AttachPolicy` on `cert/*`.

`GreengrassV2IoTThingPolicy` is attached to every Greengrass device certificate in the account. With `iot:CreatePolicyVersion` anyone holding a setup token can make a new default version that allows `iot:*` on `*`. Every device certificate in the account then gets full IoT data-plane access, and `iot:AssumeRoleWithCertificate` on every role alias in the account. With `iot:AttachPolicy` on `cert/*` they can attach any existing IoT policy to any certificate.

A live Quick Setup run on 2026-10-29 in the least-privilege test account (#110 on a fresh account) also showed that the station cannot do the one write it needs. `setup_station.sh` adds a `ShadowManagerHttpsDataPlaneSync` statement to the thing policy. On Quick Setup it fails with "Could not verify GreengrassV2IoTThingPolicy — the check itself failed (... AccessDeniedException ... GetPolicyVersion)": the session policy has no `iot:GetPolicyVersion`. So Quick Setup devices never get the shadow statement from the station, and ShadowManager cloud sync depends on whatever the policy already holds.

This feature moves the IoT policy writes to the portal, as #110 did for the IAM writes. The station keeps only the IoT actions that create and register its own certificate and thing.

## Glossary

- **Thing_Policy**: the IoT policy `GreengrassV2IoTThingPolicy`, shared by every Greengrass device certificate in the account.
- **TES_Certificate_Policy**: the IoT policy `GreengrassTESCertificatePolicyGreengrassCoreTokenExchangeRoleAlias` that the installer creates and attaches so a certificate may call `iot:AssumeRoleWithCertificate` on the token exchange role alias.
- **Shadow_Statement**: the `ShadowManagerHttpsDataPlaneSync` statement `setup_station.sh` adds to the Thing_Policy (`iot:GetThingShadow`, `iot:UpdateThingShadow`, `iot:DeleteThingShadow` on `arn:aws:iot:*:*:thing/*`, no policy variables).
- **Station_Session_Policy**: the session policy `build_session_policy` builds for the station's credentials.
- **Preparation_Session**: the Lambda-only session `quick_setup` already uses for the token exchange role (`build_tes_preparation_policy`).
- **Device_Certificate**: the certificate the station's provisioning creates for its thing.

## Requirements

### Requirement 1: The portal prepares the shared IoT policies

**User Story:** As a portal administrator, I want the portal to create and maintain the shared IoT policies, so that no setup token can change what every device in the account may do.

#### Acceptance Criteria

1. WHEN a station exchanges a valid setup token, THE Quick_Setup_Endpoint SHALL ensure, in a Preparation_Session, that the Thing_Policy exists. A missing Thing_Policy SHALL be created with the document the Greengrass installer would create, plus the Shadow_Statement.
2. WHEN the Thing_Policy exists and its default version lacks the Shadow_Statement, THE Quick_Setup_Endpoint SHALL add a new default version that keeps every existing statement and adds the Shadow_Statement. When five versions exist, it SHALL delete the oldest non-default version first.
3. WHEN the Thing_Policy's default version already holds the Shadow_Statement, THE Quick_Setup_Endpoint SHALL leave the policy unchanged.
4. THE Quick_Setup_Endpoint SHALL ensure that the TES_Certificate_Policy exists, with the document the installer would create, scoped to the account's token exchange role alias.
5. IF a preparation step fails, THEN THE Quick_Setup_Endpoint SHALL return `503 credential_issuance_failed` and leave the setup token unconsumed, as for the token exchange role.
6. THE Preparation_Session SHALL be narrowed to these two policy names and the actions above.

### Requirement 2: The station cannot change IoT policies

**User Story:** As a security reviewer, I want the station's credentials to hold no IoT policy write, so that a leaked setup token cannot widen device permissions.

#### Acceptance Criteria

1. THE Station_Session_Policy SHALL NOT allow `iot:CreatePolicy`, `iot:CreatePolicyVersion`, `iot:DeletePolicyVersion`, `iot:ListPolicyVersions`, `iot:SetDefaultPolicyVersion` or `iot:DeletePolicy` on any resource.
2. THE Station_Session_Policy SHALL keep `iot:GetPolicy` on the Thing_Policy and the TES_Certificate_Policy, which the installer calls before it would create them.
3. WHEN the portal reports that the IoT policies are prepared, `run.sh` SHALL tell `setup_station.sh`, and `setup_station.sh` SHALL skip its own Thing_Policy check and repair without a warning.
4. A manual install (no Quick Setup) SHALL keep today's Thing_Policy check and repair.

### Requirement 3: The station attaches only the two known policies to its own certificate

**User Story:** As a security reviewer, I want `iot:AttachPolicy` limited to the station's own certificate and the two DDA policies, so that a station cannot attach another IoT policy to any certificate.

#### Acceptance Criteria

1. THE design SHALL choose between (a) keeping `iot:AttachPolicy` on the station, limited as far as IoT authorization allows, and (b) moving certificate creation and policy attachment to the portal, so the station gets no `iot:AttachPolicy` at all (Requirement 4).
2. IF option (a) is chosen, THEN THE design SHALL record which part of the risk remains. IoT authorizes `iot:AttachPolicy` on the target certificate only, not on the policy.

### Requirement 4: Certificate creation without IoT writes on the station (if chosen)

**User Story:** As a station operator, I want the device's private key to stay on the device, and the station to need no IoT write permissions.

#### Acceptance Criteria

1. WHERE option (b) of Requirement 3 is chosen, THE station SHALL generate its key pair and a certificate signing request locally, and the private key SHALL never leave the station.
2. WHERE option (b) is chosen, THE Quick_Setup_Endpoint SHALL accept the CSR with the setup token. It SHALL then:
   - create the certificate (`iot:CreateCertificateFromCsr`);
   - attach the Thing_Policy and the TES_Certificate_Policy;
   - create the registered thing and attach the certificate to it;
   - add the thing to its Device_Group;
   - return the certificate, the IoT data and credential endpoints, and the role alias name.
3. WHERE option (b) is chosen, THE station SHALL run the Greengrass installer with `--provision false` and a configuration built from that response, and its credentials SHALL hold no IoT write action. The completion report SHALL be unchanged.
4. THE Quick_Setup_Endpoint SHALL accept a CSR only for the registration its token is bound to, at most once.

### Requirement 5: Compatibility and rollout

#### Acceptance Criteria

1. A station that downloaded the previous bundle and exchanges its token after the portal is updated SHALL still complete provisioning, possibly with warnings.
2. Accounts whose Thing_Policy was changed by hand SHALL keep every existing statement (Requirement 1.2).
3. THE portal's IAM grants (DDAStationProvisioningRole, the Lambda role) SHALL go through the IAM synth gate with approved additions and removals.
