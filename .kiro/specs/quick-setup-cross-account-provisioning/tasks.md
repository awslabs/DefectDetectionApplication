# Implementation Plan: cross-account Quick Setup provisioning

## Overview

A dedicated `DDAStationProvisioningRole` in each use-case account, assumed by the `quick_setup` Lambda for cross-account use cases. Decision point: keep or drop the `DDAPortalAccessRole` fallback (design.md recommends dropping it).

## Tasks

- [ ] 1. Share the provisioning Ceiling
  - Move the `StationProvisioningRole` statements of `compute-stack.ts` into a `lib/` helper that takes the account; use it from `compute-stack.ts` with no template change (IAM synth gate unchanged).
  - _Requirements: 1.1_

- [ ] 2. The role in the UseCase account stack
  - `DDAStationProvisioningRole` with the shared Ceiling, 1-hour sessions, trust on the portal account with `aws:PrincipalArn` (the QuickSetupHandler role pattern) and `sts:ExternalId`.
  - Jest: trust, Ceiling equal to the portal's.
  - _Requirements: 1.1, 1.2_

- [ ] 3. Portal side
  - `QuickSetupHandlerRole`: `sts:AssumeRole` on each trusted account's `DDAStationProvisioningRole`.
  - `quick_setup.exchange_credentials`: derive the dedicated role ARN for cross-account use cases; full preparation (`create_missing=True`); `503 provisioning_role_unavailable` when it cannot be assumed, token kept; `run.sh` prints the remedy.
  - Decide and implement the fallback question.
  - _Requirements: 1.3, 2.1-2.4, 3.1, 3.2_

- [ ] 4. Least-privilege install
  - `usecase-cfn-execution-policy.json`: role actions on `role/DDAStationProvisioningRole`; boundary compliance if the boundary covers UseCase accounts by then.
  - `setup-account.sh usecase --dry-run` shows the change; setup_account.bats.
  - _Requirements: 1.4, 4.2, 4.3_

- [ ] 5. Tests and docs
  - Backend moto tests for the cross-account exchange and the missing role; README and ADMIN_GUIDE rollout note.
  - _Requirements: 2, 4.1_

- [ ] 6. Live cross-account Quick Setup
  - Portal account plus one use-case account, both stacks deployed, a fresh station in a use-case account without a token exchange role; CloudTrail on both sides.
  - _Requirements: 4.4_
