# Implementation Plan: Quick Setup without IoT policy writes on the station

## Overview

Phase 1 (tasks 1-6) moves the shared IoT policy writes to the portal. Phase 2 (tasks 7-10) is the decision point of design.md: a certificate from a CSR and no IoT write on the station. Start phase 2 only once the user chooses it.

## Tasks

- [ ] 1. Add `edge-cv-portal/backend/functions/iot_policies.py`
  - Thing_Policy name and the installer's default document, `SHADOW_STATEMENT`, the TES_Certificate_Policy document.
  - `prepare(iot, region, account_id)`: create or augment the Thing_Policy (keep every statement; at five versions delete the oldest non-default one; one retry on a version conflict); create the TES_Certificate_Policy when missing.
  - _Requirements: 1.1-1.4_

- [ ] 2. Use it from `quick_setup.exchange_credentials`
  - Rename `_prepare_token_exchange_role` to `_prepare_station_resources`; one Preparation_Session for both preparations.
  - `build_tes_preparation_policy`: the six IoT policy actions on the two exact policy ARNs.
  - Response field `iot_policies_prepared`; failures return 503 with the token kept.
  - _Requirements: 1.5, 1.6_

- [ ] 3. Narrow the station's session policy
  - Remove `iot:CreatePolicy`, `iot:CreatePolicyVersion`, `iot:DeletePolicyVersion`, `iot:ListPolicyVersions` from `build_session_policy`; keep `iot:GetPolicy` and `iot:AttachPolicy`.
  - Update `test_property_session_policy_scoping.py`'s fixed action set.
  - _Requirements: 2.1, 2.2_

- [ ] 4. Infrastructure
  - `DDAStationProvisioningRole`: `iot:GetPolicyVersion` on the Thing_Policy; IAM synth gate record and approval.
  - _Requirements: 5.3_

- [ ] 5. Station
  - `run.sh`: `DDA_IOT_POLICIES_PREPARED` from the response.
  - `setup_station.sh`: skip the Thing_Policy check and repair when it is 1, keeping the line count; rebaseline `setup_station_unfixed_prefix.txt` and `dependency_baseline_setup_station.txt`.
  - _Requirements: 2.3, 2.4, 5.1_

- [ ] 6. Tests and live check
  - moto tests for `iot_policies.prepare` (create, augment, five versions, retry, existing TES certificate policy, statement parity with `iot_policy_shadow_statement.py`); exchange tests; `run.bats`; a section test for the skipped block.
  - Live: Quick Setup on a fresh account and on one whose Thing_Policy lacks the Shadow_Statement; CloudTrail shows no IoT policy write by the station session; ShadowManager sync works on the device.
  - _Requirements: 1-2, 5.1, 5.2_

- [ ] 7. (Phase 2) Station key and CSR
  - `run.sh` generates the key (mode 600, Greengrass root) and CSR; the exchange body carries `csr`.
  - _Requirements: 4.1_

- [ ] 8. (Phase 2) Certificate, thing and group in the Lambda
  - After the token's conditional consume: `CreateCertificateFromCsr`, both `AttachPolicy` calls on that certificate, `CreateThing`, `AttachThingPrincipal`, `AddThingToThingGroup`, `DescribeEndpoint`; the response fields of design.md; failure after the consume marks the registration `failed`.
  - _Requirements: 3.1, 4.2, 4.4_

- [ ] 9. (Phase 2) Installer without provisioning
  - `setup_station.sh` with `--provision false` and an `--init-config` from the response; decide option (i) or (ii) for the core device tag and the group check, and implement it.
  - _Requirements: 4.3_

- [ ] 10. (Phase 2) Remove the station's IoT writes and test
  - Session policy without `iot:CreateKeysAndCertificate`, `iot:AttachPolicy`, `iot:CreateThing`, `iot:AttachThingPrincipal` (or no station credentials under option ii); tests; live Quick Setup with CloudTrail check.
  - _Requirements: 3, 4_
