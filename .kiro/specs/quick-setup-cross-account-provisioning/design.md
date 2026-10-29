# Design: cross-account Quick Setup provisioning

## Overview

Every Use_Case_Account gets a `DDAStationProvisioningRole` created by the UseCase account stack. The `quick_setup` Lambda assumes it, with the use case's external id, for cross-account use cases. Both the station's credentials and the Lambda's Preparation_Session use it. The rest of Quick Setup is unchanged: the same session policies and the same preparation code. `token_exchange_role.prepare(..., create_missing=True)` then runs for every use case. `DDAPortalAccessRole` is not widened.

## Why a second role rather than widening DDAPortalAccessRole

- `DDAPortalAccessRole` is assumed by most portal Lambdas for everyday work. Adding `iam:CreateRole`, `iam:AttachRolePolicy`, `iot:CreateKeysAndCertificate` and the like to it would give all of them those actions.
- A role used only by `quick_setup` keeps the Ceiling where it is needed. Every session from it is narrowed by a session policy, as in the portal account.
- The portal account already works this way for same-account use cases. Cross-account then becomes the same code path with a different role ARN.

## Use-case account stack

`usecase-account-stack.ts` creates:

- `DDAStationProvisioningRole` (fixed name, as in the portal account). It trusts the portal account's `QuickSetupHandlerRole`, which has a CloudFormation-generated name. To stay stable across portal redeploys, the trust names the portal account root with conditions:
  - `aws:PrincipalArn` like `arn:aws:iam::<portal>:role/EdgeCVPortalComputeStack-QuickSetupHandlerRole*`;
  - `sts:ExternalId` equal to the use case's external id.

  The external id is already a stack parameter, as for `DDAPortalAccessRole`.
- The Ceiling, copied from `compute-stack.ts`'s `StationProvisioningRole` statements:
  - IoT thing and thing group;
  - certificate, policy, endpoint and role-alias actions;
  - token exchange role setup on `GreengrassV2TokenExchangeRole*` and `GreengrassV2TokenExchangeRoleAccess*`;
  - `greengrass:TagResource` on core devices;
  - `sts:GetCallerIdentity`.

  The statements move into a shared helper in `lib/` so the two stacks cannot drift. A jest test pins them equal.
- `maxSessionDuration` of 1 hour, as in the portal account.

## Portal

- `compute-stack.ts`: `QuickSetupHandlerRole` gains `sts:AssumeRole` on `arn:aws:iam::<id>:role/DDAStationProvisioningRole` for each trusted use-case account. It already has it on each account's `DDAPortalAccessRole`.
- `quick_setup.exchange_credentials`:
  - **Cross-account role**: the role ARN for a cross-account use case becomes `arn:aws:iam::<account_id>:role/DDAStationProvisioningRole`. This is derived from the use case's account id, not stored. The external id is unchanged. `same_account` stays a separate flag: the TES preparation now gets `create_missing=True` for both.
  - **Missing role**: an `AccessDenied` from `AssumeRole` on that role means the account predates this feature or the stack failed. It returns `503 provisioning_role_unavailable` with a message naming the UseCase account stack. The token stays unconsumed. `run.sh` prints the message.
- The `DDAPortalAccessRole` fallback for the inline-only TES preparation is removed once every trusted account has the role. Until then it stays behind a check of the use case record (`station_provisioning_role: "dedicated" | "legacy"`), set when the account stack is redeployed. Keeping the fallback is a decision point. The simpler alternative is to fail with the remedy above and drop the fallback; it is recommended, because cross-account Quick Setup does not work today anyway.

## Policies and least-privilege install

- **UseCase execution policy** (`usecase-cfn-execution-policy.json`): `iam:CreateRole`, `iam:PutRolePolicy` and the other role actions on `role/DDAStationProvisioningRole`. If the boundary follow-up has been extended to the UseCase account, the role carries that boundary. Its Ceiling must then fit within the boundary, including the token exchange role exemption.
- **UseCase installer policy**: no change. The installer only deploys the stack.
- **README**: one paragraph under the multi-account setup.

## Testing

- Jest:
  - the new role, its trust (principal pattern and external id) and its Ceiling, equal to the portal's;
  - `QuickSetupHandlerRole` may assume only the trusted accounts' provisioning roles.
- Backend (moto):
  - a cross-account exchange assumes the dedicated role for both sessions and runs the full preparation;
  - a missing role returns `503 provisioning_role_unavailable` with the token kept.
- Live: a portal account plus one use-case account; deploy both stacks; run Quick Setup on a fresh station in a use-case account that has no token exchange role; CloudTrail checked for denials on both sides.

## Dependencies

- The IoT thing policy spec (`quick-setup-iot-thing-policy`) adds IoT policy preparation to the same Preparation_Session. If its phase 2 (a certificate from a CSR) is chosen, the Ceiling here changes to match.
- The permissions boundary follow-up, if extended to UseCase accounts.
