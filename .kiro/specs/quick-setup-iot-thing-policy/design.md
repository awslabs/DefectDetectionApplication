# Design: Quick Setup without IoT policy writes on the station

## Overview

Two phases.

- **Phase 1** moves the two IoT policy writes into the `quick_setup` Lambda's Preparation_Session, as #110 did for the token exchange role. The station loses `iot:CreatePolicy`, `iot:CreatePolicyVersion`, `iot:DeletePolicyVersion` and `iot:ListPolicyVersions`. That closes the most severe path: rewriting the Thing_Policy of every device. Phase 1 is small and keeps the Greengrass installer's `--provision true` flow.
- **Phase 2** (recommended, Requirement 3 option b) moves certificate creation and policy attachment to the portal through a certificate signing request. The station then needs no IoT write action, and its private key never leaves it. That also closes `iot:AttachPolicy` on `cert/*`, which phase 1 cannot limit. IoT authorizes `AttachPolicy` on the target only, so the policy name cannot be restricted.

Phase 2 is a larger change to the station flow, so it is a decision point. The tasks file covers phase 1 in full and phase 2 as a separate block.

## What the station does today (from the installer and setup_station.sh)

The Greengrass installer with `--provision true --thing-policy-name GreengrassV2IoTThingPolicy`:

1. `iot:GetPolicy` on the Thing_Policy; `iot:CreatePolicy` with its default document when missing.
2. `iot:CreateKeysAndCertificate`, `iot:AttachPolicy` (Thing_Policy to the new certificate), `iot:CreateThing`, `iot:AttachThingPrincipal`.
3. Thing group: describe, create when missing, add the thing.
4. `iot:DescribeRoleAlias`. The alias exists since #110, so no role or alias creation.
5. `iot:GetPolicy` on the TES_Certificate_Policy; `iot:CreatePolicy` when missing; `iot:AttachPolicy` to the certificate.
6. `iot:DescribeEndpoint` (data and credentials).

Then `setup_station.sh` (lines ~1057-1172) checks the Thing_Policy for the Shadow_Statement:

- `iot:GetPolicy` for the default version id, then `iot:GetPolicyVersion`;
- `iot_policy_shadow_statement.py` builds the augmented document;
- `iot:ListPolicyVersions` and `iot:DeletePolicyVersion` when five versions exist;
- `iot:CreatePolicyVersion --set-as-default`.

On Quick Setup the `GetPolicyVersion` call is denied (2026-10-29 run), so the repair never happens.

The installer's default Thing_Policy document is broad:

```json
{"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
  "Action": ["iot:Publish", "iot:Subscribe", "iot:Receive", "iot:Connect", "greengrass:*"],
  "Resource": ["*"]}]}
```

Phase 1 reproduces it exactly, plus the Shadow_Statement, so Quick Setup devices get the same policy as a manual install. Narrowing it is out of scope; it would change every device.

## Phase 1

### Backend

- A new module `iot_policies.py` (pure plus boto3 calls, like `token_exchange_role.py`) holds:
  - `THING_POLICY_NAME`, `INSTALLER_THING_POLICY_DOCUMENT`, `SHADOW_STATEMENT` (the same statement `iot_policy_shadow_statement.py` adds; a test parses both so they cannot drift);
  - `tes_certificate_policy_document(region, account_id)`;
  - `prepare(iot, region, account_id)`.
- What `prepare` does:
  - **Thing_Policy missing**: create it with the installer document plus the Shadow_Statement.
  - **Thing_Policy present**: read the default version (`GetPolicyVersion`). If it lacks the Shadow_Statement, delete the oldest non-default version when five exist, then create a new default version with every existing statement plus the Shadow_Statement. A `VersionsLimitExceededException` or `VersionConflictException` from a concurrent exchange gets one re-read and retry.
  - **TES_Certificate_Policy**: create it when missing. An existing one is left as it is.
- `quick_setup._prepare_token_exchange_role` becomes `_prepare_station_resources`. It runs both preparations in one Preparation_Session. `build_tes_preparation_policy` gains these actions on the two policy ARNs (exact names, the use case region): `iot:GetPolicy`, `iot:GetPolicyVersion`, `iot:CreatePolicy`, `iot:ListPolicyVersions`, `iot:CreatePolicyVersion`, `iot:DeletePolicyVersion`.
- The response gains `iot_policies_prepared` (true or false). A failure returns `503 credential_issuance_failed` with the token kept.
- `build_session_policy` drops `iot:CreatePolicy`, `iot:CreatePolicyVersion`, `iot:DeletePolicyVersion` and `iot:ListPolicyVersions`. It keeps `iot:GetPolicy`, which the installer calls, and `iot:AttachPolicy` (phase 2 removes it).

### Infrastructure

- `DDAStationProvisioningRole` gains `iot:GetPolicyVersion` on the Thing_Policy. It already has the rest.
- The IAM synth gate records the addition. The removal from the session policy is not CDK.

### Station

- `run.sh` passes `DDA_IOT_POLICIES_PREPARED=1` when the response says so.
- `setup_station.sh` skips the "Ensuring IoT thing policy allows shadow data-plane sync" block with a "prepared by the Edge CV Portal" line.
- The edit must keep the script's line count. `csi_nvargus_optional/goldens/setup_station_unfixed_prefix.txt` pins lines 1-1581, and the edit is rebaselined there and in `dependency_baseline_setup_station.txt`, as #110 did.

### Cross-account use cases

`DDAPortalAccessRole` has no IoT policy permission. Cross-account preparation therefore depends on the cross-account provisioning spec (`quick-setup-cross-account-provisioning`), which gives the use-case account its own `DDAStationProvisioningRole`. Until then a cross-account use case reports `iot_policies_prepared: false`, and the station keeps today's behaviour.

## Phase 2 (decision point): certificate from a CSR

- **Station**: `run.sh` generates an EC P-256 key and a CSR with `openssl` before the exchange. The key is written with mode 600 to the Greengrass root and never sent anywhere.
- **Exchange**: `POST /quick-setup/credentials` accepts an optional `csr`. With it, the Lambda runs in its Preparation_Session (extended to the actions below):
  - `iot:CreateCertificateFromCsr` (active);
  - `iot:AttachPolicy` for the Thing_Policy and the TES_Certificate_Policy, on that certificate only;
  - `iot:CreateThing`;
  - `iot:AttachThingPrincipal`;
  - `iot:AddThingToThingGroup`;
  - `iot:DescribeEndpoint`.

  The response carries `certificate_pem`, `iot_data_endpoint`, `iot_cred_endpoint`, `role_alias` and `thing_name`. The certificate is created only after the token's conditional consume succeeds, so at most one certificate exists per token. A failure after the consume marks the registration `failed` with a reason.
- **Installer**: `setup_station.sh` runs it with `--provision false` and an `--init-config` that names the certificate, key, root CA and endpoints.
- **Station credentials**: the station still needs some AWS calls: the core device tag (`greengrass:TagResource`) and the Device_Group check in `run.sh`. Two options:
  - (i) keep short-lived credentials with only those two actions;
  - (ii) let the portal tag the core device and check the group when the completion report arrives.

  Option (ii) removes AWS credentials from the station entirely. Recommended, but it changes the completion handler. It needs `greengrass:TagResource` and `iot:ListThingGroupsForThing` in the Lambda's session.
- **Backwards compatibility**: a bundle without CSR support keeps working until the portal stops accepting exchanges without a CSR (a later release).

## Testing

- Unit (moto IoT supports policies and versions):
  - creation;
  - adding the statement;
  - the five-version limit;
  - the retry on a concurrent version;
  - an existing TES_Certificate_Policy left alone;
  - parity of `SHADOW_STATEMENT` with `iot_policy_shadow_statement.py`.
- Property test: the session policy's fixed action set loses the four IoT writes.
- `run.bats` and `setup_station_tes_section.bats`-style tests for the new flag and the skipped block.
- Live: Quick Setup on a fresh account and on an account whose Thing_Policy lacks the Shadow_Statement. Check the CloudTrail trace of the station session (no IoT policy write) and ShadowManager sync on the device.
