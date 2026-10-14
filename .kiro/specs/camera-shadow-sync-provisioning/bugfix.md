# Bugfix Requirements Document

## Introduction

The portal device page Cameras tab showed "Device has no camera registry shadow to refresh from" and "Never synced" with Cameras (0) for Greengrass core device `ryanorinagxdevkithomelabjp622`, even though the edge Edge_Sync_Agent (`src/backend/camera_sync/agent.py`) was successfully writing the local `dda-camera-registry` named shadow and the Basler USB camera was detected and acquiring frames.

Root-cause diagnosis (confirmed empirically) found two independent provisioning gaps:

**Gap 1 — device-side IoT policy (wrong authorization layer).** `aws.greengrass.ShadowManager` cloud sync calls the IoT data plane over HTTPS (port 8443) authenticated with the device X.509 certificate, so it is authorized by the IoT policy attached to the certificate — not by the IAM role from the token exchange service. The device certificate's `GreengrassV2IoTThingPolicy` granted `iot:GetThingShadow`/`iot:UpdateThingShadow`/`iot:DeleteThingShadow` only on `arn:aws:iot:*:*:thing/${iot:Connection.Thing.ThingName}`. Thing policy variables only resolve on MQTT connections, never over HTTPS, so every `CloudUpdateSyncRequest` failed with `ForbiddenException` 403 (confirmed in greengrass.log and reproduced with a raw curl using the device certificate). `station_install/setup_station.sh` step 3.6 attempted to fix camera sync by attaching a `ShadowManagerSyncPolicy` IAM policy to `GreengrassV2TokenExchangeRole` — the wrong layer, with no effect on ShadowManager sync (IAM simulation said allowed while the device still got 403).

**Gap 2 — cloud-side missing IoT topic rule in single-account deployments.** The IoT topic rule `dda_camera_registry_shadow_documents` (forwarding `$aws/things/+/shadow/name/dda-camera-registry/update/documents` events to the `dda-portal-camera-shadow-reports` SQS queue) is defined only in `edge-cv-portal/infrastructure/lib/usecase-account-stack.ts` (`CameraRegistryShadowRule` + `DDACameraShadowRuleRole`). That stack is only deployed for cross-account use-case onboarding. In the common single-account setup (portal account == use-case account), no UsecaseAccountStack exists, so the rule was never created and shadow reports never reached the ingest queue/Lambda. The analogous `dda_user_accounts_shadow_documents` rule is provisioned in `compute-stack.ts` (`UserAccountsShadowRule`) and exists in the account.

The live production fix has already been applied manually (IoT policy version 3 with an HTTPS-compatible shadow statement; manually created topic rule and role) and verified end to end. This spec covers the durable repository fixes so that fresh provisioning produces a working system, plus collision avoidance between the manually-created/ComputeStack rule and a future UsecaseAccountStack deployment in the same account.

## Bug Analysis

### Current Behavior (Defect)

Gap 1 — station provisioning grants shadow sync at the wrong layer:

1.1 WHEN `setup_station.sh` step 3.6 provisions shadow sync permissions THEN the system attaches the `ShadowManagerSyncPolicy` IAM inline policy to `GreengrassV2TokenExchangeRole`, which ShadowManager cloud sync never uses, leaving the device certificate's IoT policy without an HTTPS-compatible shadow grant

1.2 WHEN ShadowManager attempts cloud sync over HTTPS with a device certificate whose IoT policy only grants shadow actions on `thing/${iot:Connection.Thing.ThingName}` THEN the system rejects every `CloudUpdateSyncRequest` with `ForbiddenException` 403, because thing policy variables do not resolve on HTTPS connections

1.3 WHEN ShadowManager cloud sync fails persistently with 403 THEN the portal shows the device as "Never synced" with Cameras (0) and camera refresh fails with "Device has no camera registry shadow to refresh from", despite the edge agent correctly writing the local named shadow

Gap 2 — camera shadow topic rule missing in single-account deployments:

1.4 WHEN the portal infrastructure is deployed in a single-account setup (portal account == use-case account, no UsecaseAccountStack) THEN the system never creates the `dda_camera_registry_shadow_documents` IoT topic rule, so `dda-camera-registry` shadow documents events are never forwarded to the `dda-portal-camera-shadow-reports` queue and automatic camera registry ingest never happens

1.5 WHEN a future UsecaseAccountStack deployment occurs in an account that already has the manually created (or ComputeStack-provisioned) `dda_camera_registry_shadow_documents` rule and `DDACameraShadowRuleRole` role THEN the system fails with a name collision, because the usecase-account-stack.ts definitions use those same fixed names

### Expected Behavior (Correct)

Gap 1 — station provisioning grants shadow sync at the IoT policy layer:

2.1 WHEN `setup_station.sh` step 3.6 provisions shadow sync permissions THEN the system SHALL ensure the Greengrass core device's IoT policy (attached to the device certificate) includes an HTTPS-compatible statement allowing `iot:GetThingShadow`, `iot:UpdateThingShadow`, and `iot:DeleteThingShadow` on `arn:aws:iot:*:*:thing/*` (no thing policy variables), e.g. by creating a new default version of `GreengrassV2IoTThingPolicy` that adds the statement while preserving the existing statements

2.2 WHEN ShadowManager attempts cloud sync over HTTPS with a device certificate carrying the provisioned IoT policy THEN the system SHALL authorize the shadow calls (HTTP 200) and the `dda-camera-registry` named shadow SHALL mirror to IoT Core

2.3 WHEN the IoT policy grant is provisioned and cloud sync succeeds THEN the portal SHALL show the device's cameras with a synced status once shadow reports flow through ingest

Gap 2 — camera shadow topic rule provisioned for the single-account case:

2.4 WHEN the portal infrastructure is deployed in a single-account setup THEN the system SHALL provision the `dda_camera_registry_shadow_documents` IoT topic rule (SQL `SELECT *, topic(3) AS thing_name FROM '$aws/things/+/shadow/name/dda-camera-registry/update/documents'`) and an IAM role allowing `sqs:SendMessage` to the `dda-portal-camera-shadow-reports` queue in `compute-stack.ts`, mirroring the existing `UserAccountsShadowRule` pattern

2.5 WHEN both the ComputeStack provisioning and a UsecaseAccountStack deployment could apply to the same account THEN the system SHALL avoid resource name collisions between the two definitions (e.g., only create the usecase-account-stack.ts copy when the use-case account differs from the portal account, or deduplicate the fixed names)

2.6 WHEN a device publishes a `dda-camera-registry` shadow documents event in a single-account deployment THEN the system SHALL deliver the event to the `dda-portal-camera-shadow-reports` queue and the ingest Lambda (`camera_sync.py`) SHALL update the `dda-portal-camera-registry` table automatically, without requiring a manual refresh

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a device connects over MQTT THEN the system SHALL CONTINUE TO authorize shadow operations via the existing thing-policy-variable statement in `GreengrassV2IoTThingPolicy` (the existing statement is preserved, not replaced)

3.2 WHEN `setup_station.sh` runs its other provisioning steps (token exchange role, ECR access, policy verification) THEN the system SHALL CONTINUE TO provision them as before

3.3 WHEN the UsecaseAccountStack is deployed for a genuine cross-account use-case account (use-case account != portal account) THEN the system SHALL CONTINUE TO provision the camera shadow topic rule and role in that account, forwarding shadow reports to the portal account's queue

3.4 WHEN the `dda_user_accounts_shadow_documents` rule (`UserAccountsShadowRule`) and account-sync ack flow operate THEN the system SHALL CONTINUE TO work unchanged

3.5 WHEN the edge-side camera sync code (`src/backend/camera_sync/`) runs THEN the system SHALL CONTINUE TO behave exactly as it does today (no edge code changes)

3.6 WHEN the on-demand camera refresh route is invoked THEN the system SHALL CONTINUE TO refresh the camera registry from the cloud shadow as before

3.7 WHEN the infrastructure jest suite and `npx tsc --noEmit` run THEN the system SHALL CONTINUE TO pass

3.8 WHEN the security preservation suite pins `setup_station.sh` via `test/backend-test/security/baselines/dependency_baseline_setup_station.txt` THEN the system SHALL CONTINUE TO enforce the baseline (rebaselined to the new content, never weakened or disabled)

3.9 WHEN the portal backend tests (including `test_camera_shadow_sync_integration.py`, which documents the rule SQL) run THEN the system SHALL CONTINUE TO pass

## Bug Condition (for validation)

**Bug Condition Function — Gap 1:**
```pascal
FUNCTION isBugCondition_Gap1(X)
  INPUT: X of type ProvisionedCoreDevice
  OUTPUT: boolean

  // A core device whose certificate's IoT policy lacks an HTTPS-compatible
  // (non-thing-policy-variable) allow for the shadow actions on the thing
  RETURN NOT existsStatement(X.certIotPolicy,
    actions ⊇ {iot:GetThingShadow, iot:UpdateThingShadow, iot:DeleteThingShadow}
    AND resource covers X.thingArn without thing policy variables)
END FUNCTION
```

**Bug Condition Function — Gap 2:**
```pascal
FUNCTION isBugCondition_Gap2(X)
  INPUT: X of type PortalDeployment
  OUTPUT: boolean

  // Single-account deployment where no UsecaseAccountStack exists
  RETURN X.portalAccountId = X.usecaseAccountId
    AND NOT topicRuleExists(X, 'dda_camera_registry_shadow_documents')
END FUNCTION
```

**Property Specification — Fix Checking:**
```pascal
// Property: Fix Checking — Gap 1 (IoT policy layer grant)
FOR ALL X WHERE isBugCondition_Gap1(X) DO
  result ← provisionStation'(X)   // setup_station.sh step 3.6 after fix
  ASSERT certIotPolicy(result) contains HTTPS-compatible shadow statement
  ASSERT shadowManagerCloudSync(result) succeeds (no 403)
END FOR

// Property: Fix Checking — Gap 2 (single-account rule provisioning)
FOR ALL X WHERE isBugCondition_Gap2(X) DO
  result ← deployComputeStack'(X)  // compute-stack.ts after fix
  ASSERT topicRuleExists(result, 'dda_camera_registry_shadow_documents')
  ASSERT ruleDeliversTo(result, 'dda-portal-camera-shadow-reports')
  ASSERT noNameCollision(result, futureUsecaseAccountStackDeploy(X))
END FOR
```

**Preservation Goal:**
```pascal
// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition_Gap1(X) AND NOT isBugCondition_Gap2(X) DO
  ASSERT F(X) = F'(X)
  // MQTT thing-policy-variable auth, other setup_station.sh steps,
  // cross-account UsecaseAccountStack provisioning, UserAccountsShadowRule,
  // edge camera_sync code, manual refresh route, and all existing test
  // suites behave identically before and after the fix
END FOR
```
