# Implementation Plan

## Overview

This plan fixes the two camera-shadow-sync provisioning gaps using the exploratory bugfix workflow:
surface both gaps on UNFIXED code first (Properties 1–4: Bug Condition), capture existing behavior
that must not change (Properties 5–7: Preservation), apply the fixes, then validate and confirm no
regressions. All exploration and preservation tests are written and run against the UNFIXED code
before any fix is applied.

**Gap 1 (device-side, wrong authorization layer)**: the `setup_station.sh` thing-policy ensure block
(~lines 1101–1195) writes an HTTPS-incompatible shadow statement (`${iot:Connection.Thing.ThingName}`
thing policy variable — resolves only on MQTT, never on the HTTPS 8443 data plane ShadowManager
actually uses) and its `grep -q "iot:UpdateThingShadow"` idempotency check reports "already granted"
for that very statement, so re-runs never repair a broken policy. The fix extracts the decision logic
into a new pure-Python helper (`station_install/iot_policy_shadow_statement.py`, property-tested with
Hypothesis) and rewrites the ensure block to append the `ShadowManagerHttpsDataPlaneSync` statement to
the *current* default document (preserving all existing statements, exactly like the verified manual
production fix in account 164152369890). Step 3.6's IAM `put-role-policy` stays byte-identical (D8);
only its comment/warning prose is corrected.

**Gap 2 (cloud-side, missing topic rule)**: the `dda_camera_registry_shadow_documents` IoT topic rule
exists only in `usecase-account-stack.ts` (cross-account onboarding), so single-account portal
deployments never get it and shadow reports never reach the ingest queue. The fix mirrors the
existing `UserAccountsShadowRule` pattern in `compute-stack.ts` (distinct rule name
`dda_camera_registry_shadow_documents_portal`, CDK-generated role name — D6) and gates the
UsecaseAccountStack's fixed-name copies behind a deploy-time `CfnCondition`
(`Not(Equals(portalAccountId, AWS::AccountId))` — D7, keeps the security IAM baselines for
`DDAPortalUseCaseAccountStack` byte-identical), with `STACK_VERSION` 1.5.0 → 1.6.0 (D9).

Baselines to preserve: infrastructure jest 30 passing + `npx tsc --noEmit` clean, portal backend 883
passing, LocalServer `test/backend-test` 204 passed + 3 skipped. The setup-station security golden
and the `EdgeCVPortalComputeStack` IAM baseline pair are rebaselined per D10 (never weakened; test
files untouched). The final migration task (deleting the manually created cloud resources in account
164152369890) touches live cloud resources and runs only with explicit user coordination.

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2"],
      "description": "Write tests against UNFIXED code: task 1 (Bug Condition exploration for Gaps 1 and 2) FAILS; task 2 (Preservation observation + tests) PASSES. Independent of each other."
    },
    {
      "wave": 2,
      "tasks": ["3"],
      "description": "Apply the fixes (3.1 helper, 3.2 Hypothesis property suite, 3.3 setup_station.sh ensure block + step 3.6 prose, 3.4 setup-station golden rebaseline, 3.5 compute-stack.ts rule, 3.6 usecase-account-stack.ts condition + version bump, 3.7 jest fix-checking assertions, 3.8 IAM baseline pair regeneration), then re-run task 1 (3.9) and task 2 (3.10). Depends on wave 1."
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "Checkpoint - run all four verification suites and ensure all tests pass. Depends on wave 2."
    },
    {
      "wave": 4,
      "tasks": ["5"],
      "description": "Migration cleanup of the manually created resources in account 164152369890 after the first fixed ComputeStack deploy. REQUIRES USER COORDINATION — live cloud resources; runs only with explicit go-ahead. Depends on wave 3."
    }
  ]
}
```

- Tasks 1 and 2 are independent and must be completed BEFORE any fix (tests written against unfixed code).
- Task 3 depends on wave 1; sub-tasks 3.9 and 3.10 depend on 3.1–3.8.
- Task 4 depends on task 3. Task 5 depends on task 4, a deployed fixed ComputeStack, and the user's explicit go-ahead (deletes live cloud resources).

## Tasks

- [x] 1. Write bug condition exploration tests (BEFORE implementing the fix)
  - **Property 1: Bug Condition** - Gap 1: provisioning yields an HTTPS-compatible shadow grant; **Property 2: Bug Condition** - Gap 1: the ensure step is idempotent; **Property 3: Bug Condition** - Gap 2: ComputeStack provisions the camera shadow topic rule; **Property 4: Bug Condition** - Gap 2: no fixed-name collisions between the two definitions
  - **CRITICAL**: These tests MUST FAIL on unfixed code — the failures confirm both gaps exist
  - **DO NOT attempt to fix the tests or the code when they fail**
  - **NOTE**: These tests encode the expected behavior — they will validate the fix when they pass after implementation
  - **GOAL**: Surface executable counterexamples confirming the root-cause analysis (already confirmed empirically on device `ryanorinagxdevkithomelabjp622` and in account 164152369890 — these tests make the confirmation executable)
  - **Scoped PBT Approach**: Both gaps are deterministic configuration defects — scope each property to the concrete failing artifact (the unfixed script's heredoc/grep and the unfixed synthesized templates) as the testable seam; the full Hypothesis quantification over arbitrary policy documents lands with the helper in task 3.2
  - Create `test/backend-test/camera_shadow_sync/test_gap1_exploration.py` (new directory; parses `station_install/setup_station.sh` as text — no AWS calls):
    - Exploration case 1 — heredoc statement is HTTPS-compatible (`isBugCondition_Gap1`, design Bug Details): extract the policy document embedded in the thing-policy ensure block's heredoc (~lines 1170–1185) and assert its shadow statement's resource contains no `${` policy variable — FAILS on unfixed code (resource is `arn:aws:iot:*:*:thing/${iot:Connection.Thing.ThingName}`, which never resolves over HTTPS → every `CloudUpdateSyncRequest` 403s)
    - Exploration case 2 — idempotency predicate distinguishes the layers (`isBugCondition_Gap1` re-run path): assert the script's idempotency check does NOT report "already granted" for a document whose only shadow statement is variable-scoped — executable as: run the unfixed check (`grep -q "iot:UpdateThingShadow"`, ~line 1146) against exactly that document and assert it returns non-zero — FAILS on unfixed code (grep matches; this is the false-idempotency counterexample — re-running setup can never repair a broken policy)
    - Exploration case 3 — shadow sync provisioned at the right layer, not solely step 3.6: assert the script provisions the shadow grant on an IoT policy via an `aws iot create-policy-version` writing a variable-free shadow statement, not solely the `ShadowManagerSyncPolicy` IAM policy on `GreengrassV2TokenExchangeRole` (~lines 1542–1574) — FAILS on unfixed code (the only variable-free grant is the IAM one, which ShadowManager's cert-authenticated HTTPS sync never consults)
  - Create `edge-cv-portal/infrastructure/test/camera-shadow-sync-provisioning.test.ts` (new jest suite, leaving the existing 30-test files untouched) with its own `beforeAll` synth mirroring `camera-registry-infra.test.ts` (StorageStack + ComputeStack + a cross-account UseCaseAccountStack), exploration describe:
    - Exploration case 4 — ComputeStack has the camera shadow rule (`isBugCondition_Gap2`): assert the synthesized ComputeStack template contains an `AWS::IoT::TopicRule` whose SQL selects from `$aws/things/+/shadow/name/dda-camera-registry/update/documents` — FAILS on unfixed code (only `dda_user_accounts_shadow_documents` exists; the report queue sits with zero traffic)
    - Exploration case 5 — UsecaseAccountStack fixed names are gated (Req 1.5 collision): assert the `DDACameraShadowRuleRole` role, its default policy, and the `dda_camera_registry_shadow_documents` rule each carry a CloudFormation `Condition` in the synthesized UseCaseAccountStack template — FAILS on unfixed code (unconditional fixed names; a deploy into an account that already has the rule/role fails on create)
  - Run all tests on UNFIXED code (`PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/camera_shadow_sync/test_gap1_exploration.py`; `npx jest test/camera-shadow-sync-provisioning.test.ts` in `edge-cv-portal/infrastructure`)
  - **EXPECTED OUTCOME**: Tests FAIL (heredoc resource carries the thing policy variable; grep returns 0 on the variable-only document; no IoT-policy-layer variable-free grant; no camera-registry topic rule in ComputeStack; unconditional fixed names in UsecaseAccountStack)
  - Document counterexamples found (e.g. "unfixed heredoc shadow resource is `arn:aws:iot:*:*:thing/${iot:Connection.Thing.ThingName}` — HTTPS sync 403s even after the block 'fixes' the policy"; "`grep -q iot:UpdateThingShadow` exits 0 on the variable-only document — re-runs never repair"; "unfixed ComputeStack template has exactly one topic rule: `dda_user_accounts_shadow_documents`")
  - Mark task complete when tests are written, run, and failures are documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

- [x] 2. Write preservation tests (BEFORE implementing the fix)
  - **Property 5: Preservation** - setup_station.sh outside the fix sites, and the golden; **Property 6: Preservation** - infrastructure unchanged outside the additions; **Property 7: Preservation** - edge and backend code untouched
  - **IMPORTANT**: Follow observation-first methodology — observe behavior on UNFIXED code, record it (golden behavior), then encode it as tests that must keep passing after the fix
  - Observe on UNFIXED code: run the four baseline suites and record the counts — infrastructure jest (`npx jest` in `edge-cv-portal/infrastructure`: 30 passing) + `npx tsc --noEmit` (clean); security preservation suite (`test/backend-test/security/`) green, including `test_preservation_dependency_setup_station.py` against the current golden; portal backend suite (`edge-cv-portal/backend/tests`, 883 passing) including `test_camera_shadow_sync_integration.py` (documents the rule SQL contract); LocalServer suite (`PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test`: 204 passed + 3 skipped)
  - Observe on UNFIXED code: the content anchors in `setup_station.sh` that must survive the fix byte-identical — the installer invocation (`--thing-policy-name GreengrassV2IoTThingPolicy`, ~line 1094), the step 3.5 ECR heredoc, the step 3.6 `aws iam put-role-policy` command + its `ShadowManagerSyncPolicy` policy document (~lines 1542–1574), and the step 4 verification block
  - Add `test/backend-test/camera_shadow_sync/test_setup_station_preservation.py`: content-anchor assertions (anchors, not line numbers) that the script contains the byte-exact installer invocation, step 3.5 ECR heredoc, step 3.6 `put-role-policy` command + policy document, and step 4 verification block — PASSES on unfixed code, and must keep passing after the fix (Property 5; Requirements 3.2)
  - Add the preservation describe to `edge-cv-portal/infrastructure/test/camera-shadow-sync-provisioning.test.ts` (same new suite as task 1, assertions that pass on BOTH unfixed and fixed trees):
    - Cross-account usecase synth preservation: in the UseCaseAccountStack template (synthesized with a portalAccountId ≠ account), the `DDACameraShadowRuleRole` role name, `SendCameraShadowReports` statement (sqs:SendMessage scoped to the portal queue ARN), rule name `dda_camera_registry_shadow_documents`, its SQL, and the queue ARN/URL target equal their pre-fix values (Property 6; Requirements 3.3)
    - ComputeStack sibling preservation: `UserAccountsShadowRule` (rule name `dda_user_accounts_shadow_documents`, SQL, queue target) and the camera shadow report queue/DLQ/queue-policy properties are unchanged (Property 6; Requirements 3.4)
  - Verify (no test needed — enforced by the diff in task 3 and re-checked at 3.10): no file under `src/backend/camera_sync/` is touched, `camera_registry.py` (manual refresh) and `camera_sync.py` (ingest) are untouched — the portal backend and LocalServer suites passing unchanged is the executable form (Property 7; Requirements 3.5, 3.6, 3.9)
  - **Testing Approach**: template-level preservation uses jest equality on synthesized resources plus the security suite's existing statement-multiset machinery; the property-based preservation of the policy-ensure decision logic (compliant-policy no-op, MQTT statement survival) requires the new helper and lands with it in task 3.2
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code with the four baseline counts recorded
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9_

- [x] 3. Fix the two camera-shadow-sync provisioning gaps

  - [x] 3.1 Create the pure-Python policy helper `station_install/iot_policy_shadow_statement.py` (NEW)
    - Stdlib-only, Python 3.6-compatible (JP4 devices run Ubuntu 18.04's system python3); no AWS SDK, no network — input is a policy document JSON, output is a decision or an augmented document; sibling-file distribution follows the `edge_manager_agent_config.json` precedent (D3)
    - Define `REQUIRED_ACTIONS = frozenset({"iot:GetThingShadow", "iot:UpdateThingShadow", "iot:DeleteThingShadow"})` and `SHADOW_STATEMENT` (Sid `ShadowManagerHttpsDataPlaneSync`, Effect Allow, the three actions, Resource `arn:aws:iot:*:*:thing/*` — no policy variables; matches the verified manual production version-3 statement)
    - `_as_list(value)`: normalizes a string-or-list policy field to a list
    - Action coverage: an action entry covers a required action iff it equals the action, `"iot:*"`, or `"*"`
    - HTTPS-compatible resource: a string containing no `"${"` and either `"*"` or an `arn:aws:iot:...` ARN whose final `:`-segment is exactly `"thing/*"` (deliberately conservative — prefix-scoped resources like `thing/dda-*` do NOT satisfy the predicate; worst case is one extra appended statement, which satisfies the predicate on every later run)
    - `statement_grants_https_shadow(stmt)`: `Effect == "Allow"` AND every `REQUIRED_ACTIONS` member covered by some action entry AND at least one HTTPS-compatible resource
    - `has_https_shadow_statement(doc)`: true iff any statement in the normalized `Statement` list satisfies the above; Deny statements ignored (out of scope; document it)
    - `augment(doc)`: deep copy; normalizes `Statement` to a list; if the predicate already holds, returns the copy unchanged (defensive idempotence); otherwise appends `SHADOW_STATEMENT`; all pre-existing statements and every other top-level key (`Version`, etc.) preserved verbatim and in order
    - CLI: `python3 iot_policy_shadow_statement.py check` reads the document on stdin, exits 0 (statement present), 1 (absent), 2 (unparseable JSON / malformed document); `... augment` reads stdin, writes the augmented document JSON to stdout, exits 0 or 2
    - _Bug_Condition: isBugCondition_Gap1(X) — no HTTPS-compatible (non-thing-policy-variable) allow of the three shadow actions on the thing in the certificate IoT policy default version (from design)_
    - _Expected_Behavior: Property 1 — augment yields a document where has_https_shadow_statement holds, with every original statement preserved unchanged and in order plus SHADOW_STATEMENT; Property 2 — when the predicate already holds, augment is a no-op_
    - _Preservation: Property 5 — the MQTT thing-policy-variable statement is never removed or rewritten; augment only appends_
    - _Requirements: 2.1_

  - [x] 3.2 Write Hypothesis property + unit tests for the helper (`test/backend-test/camera_shadow_sync/test_iot_policy_statement_properties.py`)
    - Hypothesis generator over arbitrary policy documents: 0–8 statements; actions as strings or lists drawn from shadow actions, wildcards (`iot:*`, `*`), and unrelated actions; resources drawn from variable-scoped ARNs, `thing/*` ARNs, prefix ARNs (`thing/dda-*`), and `"*"`; optional Sids; `Statement` as list or single object
    - Minimum 100 iterations per property (`@settings(max_examples=100)` or higher); each property tagged in its docstring: `**Feature: camera-shadow-sync-provisioning, Property N: {property_text}**`
    - **Property 1: Bug Condition** — Gap 1: provisioning yields an HTTPS-compatible shadow grant (fix checking): `has_https_shadow_statement(augment(doc))` holds for ALL generated documents; when the predicate does not hold on `doc`, the output is `normalize(doc.Statement)` as a preserved prefix (unchanged, in order) plus exactly `SHADOW_STATEMENT` appended; non-`Statement` top-level keys unchanged. Predicate soundness: documents where every shadow-action Allow statement has `${` in all its resources → predicate false (includes the installer's MQTT-only document and the unfixed heredoc document); documents containing any variable-free `thing/*` (or `*`) Allow covering all three actions → predicate true
    - **Property 2: Bug Condition** — Gap 1: the ensure step is idempotent: `augment(augment(doc)) == augment(doc)` for ALL documents; when `has_https_shadow_statement(doc)` holds, `augment(doc) == normalize(doc)` (no write — repeated `setup_station.sh` runs are stable and never exhaust the 5-version limit); include the literal manually-deployed production version-3 document as a regression example (predicate true, augment no-op)
    - **Property 5: Preservation** — for any generated document containing the variable-scoped MQTT shadow statement (`arn:aws:iot:*:*:thing/${iot:Connection.Thing.ThingName}`), that statement appears verbatim in `augment`'s output
    - Unit tests (examples from the design): CLI exit codes 0/1/2 on present/absent/garbage stdin; `augment` output is valid JSON parseable as a policy document; installer MQTT-only document → absent; variable-only shadow document → absent; production version-3 document → present; `iot:*` / `"*"` action wildcard documents → present; `Statement` as a single object (not list) → handled
    - Run the suite (`PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/camera_shadow_sync/test_iot_policy_statement_properties.py`) — all pass against the 3.1 helper
    - _Requirements: 2.1, 2.2, 3.1_

  - [x] 3.3 Rewrite the `setup_station.sh` thing-policy ensure block (~lines 1101–1195) and correct step 3.6 prose (~lines 1542–1574)
    - Keep the existing `gg_policy_check` ok/inconclusive/absent scaffolding, `run_cmd`/`add_warning` conventions, and the bash/JMESPath 5-version pruning (delete oldest non-default version) unchanged (D5)
    - Helper resolution: `shadow_helper="$(dirname "$0")/iot_policy_shadow_statement.py"`; if missing, `add_warning` ("helper not found — cannot verify/repair the IoT policy shadow statement; ...") and skip the block's write path
    - Discovery fallback (D4): in the `absent` branch, before warning, attempt `aws iot list-thing-principals` → `aws iot list-attached-policies`; if a policy name is discovered, set `gg_thing_policy` and retry `get-policy`; only if that also fails, emit the existing warning
    - Explicit default-version read: replace the `--query policyDocument` shortcut with `get-policy --query defaultVersionId` then `get-policy-version --policy-version-id "$default_version" --query policyDocument` so the augmented document is built from what is really in force (error handling folded into the existing `gg_policy_check` classification)
    - Correct idempotency predicate: replace `elif echo "$policy_doc" | grep -q "iot:UpdateThingShadow"; then` with `elif printf '%s' "$policy_doc" | python3 "$shadow_helper" check; then` (echo "✓ ... already grants HTTPS-compatible shadow data-plane actions"); a `check` exit code of 2 routes to `add_warning` (inconclusive), NOT to the write path
    - Append instead of replace: delete the hardcoded `POLICY_EOF` heredoc; build the new version via `printf '%s' "$policy_doc" | python3 "$shadow_helper" augment > "$shadow_policy_file"` (mktemp), run the existing 5-version pruning, then `aws iot create-policy-version --policy-document file://$shadow_policy_file --set-as-default`; success echo notes existing statements preserved; failure paths use `add_warning` with the manual-repair guidance from the design; `rm -f` the temp file
    - Comment rewrite on the block: explain the HTTPS/MQTT authorization-layer distinction (ShadowManager cloud sync is HTTPS + device certificate → certificate IoT policy; thing policy variables never resolve over HTTPS; the appended statement is the HTTPS-compatible grant while the existing variable statement continues to serve MQTT)
    - Step 3.6: the `aws iam put-role-policy ... ShadowManagerSyncPolicy` command and its policy document stay **byte-identical** (D8); only the leading comment (IAM-layer belt-and-braces for SigV4 callers; ShadowManager sync does NOT use this role) and the failure `add_warning` text (no longer claims camera sync depends on it) change, per the design's substance
    - _Bug_Condition: isBugCondition_Gap1(X) — the unfixed block writes the variable-scoped statement and its grep predicate reports success on it, so provisioned devices 403 on every CloudUpdateSyncRequest and re-runs never repair (from design)_
    - _Expected_Behavior: Properties 1, 2 — the ensure step creates a new default version carrying every original statement plus ShadowManagerHttpsDataPlaneSync exactly when the predicate fails, and makes no write when it holds (from design)_
    - _Preservation: Property 5 — installer invocation, steps 1–3.5, step 3.6's put-role-policy command and policy document, and step 4 byte-identical; the MQTT variable statement never removed from any document the step writes (from design)_
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2_

  - [x] 3.4 Rebaseline the setup-station security golden
    - Recapture `test/backend-test/security/baselines/dependency_baseline_setup_station.txt` as an exact copy of the fixed `station_install/setup_station.sh` (the golden pins the whole file)
    - `test/backend-test/security/preservation/test_preservation_dependency_setup_station.py` itself is NOT modified (Req 3.8 — rebaselined, never weakened); the F1 requests-pin line is untouched by this fix, so both pin assertions keep passing
    - Run `test_preservation_dependency_setup_station.py` and confirm it passes against the recaptured golden
    - _Preservation: Property 5 — the golden remains enforced with the test file byte-identical_
    - _Requirements: 3.8_

  - [x] 3.5 Add the single-account camera shadow rule to `edge-cv-portal/infrastructure/lib/compute-stack.ts`
    - Place immediately after the `CameraSyncHandler` SQS event-source wiring (~line 1365, adjacent to the queue it feeds), mirroring `UserAccountsShadowRule`; use the design's code block: `CameraShadowRuleRole` (`iam.Role`, `assumedBy: iot.amazonaws.com`, NO `roleName` — CDK-generated, D6) with a `SendCameraShadowReports` policy statement (`sqs:SendMessage` scoped to `cameraShadowReportQueue.queueArn`), and `iot.CfnTopicRule` `CameraRegistryShadowRule` with `ruleName: 'dda_camera_registry_shadow_documents_portal'` (distinct from the usecase-account fixed name, D6), SQL `SELECT *, topic(3) AS thing_name FROM '$aws/things/+/shadow/name/dda-camera-registry/update/documents'`, `awsIotSqlVersion: '2016-03-23'`, `ruleDisabled: false`, single SQS action (`queueUrl`, `roleArn`, `useBase64: false`)
    - Include the design's migration-note code comment (single-account topology rationale; cross-account handled by the condition-gated UseCaseAccountStack; deliberately distinct names to avoid colliding with the fixed-name copies or the manually created resources)
    - No queue, queue-policy, or Lambda changes: the existing queue policy already admits the portal account via `aws:PrincipalAccount`, and `camera_sync.py` already consumes the queue; the `iot` module is already imported
    - _Bug_Condition: isBugCondition_Gap2(X) — single-account deployment with no camera-registry shadow topic rule (from design)_
    - _Expected_Behavior: Property 3 — exactly one topic rule with the exact SQL delivering to the report queue through a role granting sqs:SendMessage; Property 4 — no fixed RoleName, rule name ends `_portal` (from design)_
    - _Preservation: Property 6 — ComputeStack unchanged except the added rule + role; UserAccountsShadowRule, queue/DLQ/queue-policy, CameraSyncHandler byte-identical (from design)_
    - _Requirements: 2.4, 2.5, 2.6_

  - [x] 3.6 Gate the UsecaseAccountStack fixed-name copies and bump the version (`edge-cv-portal/infrastructure/lib/usecase-account-stack.ts`)
    - `STACK_VERSION`: `'1.5.0'` → `'1.6.0'` (D9)
    - Immediately before the existing `cameraShadowRuleRole` definition (~line 792), define `const cameraShadowCrossAccountCondition = new cdk.CfnCondition(this, 'CameraShadowCrossAccountCondition', { expression: cdk.Fn.conditionNot(cdk.Fn.conditionEquals(portalAccountId, cdk.Aws.ACCOUNT_ID)) })` with the design's rationale comment (deploy-time condition, NOT a synth-time `if` — keeps the synthesized IAM statements identical for the security preservation baselines and stays correct when the synth environment does not resolve the account, D7)
    - Assign the existing `new iot.CfnTopicRule(...)` expression to `const cameraRegistryShadowRule`; resource properties (names, SQL, queue ARN/URL target, `SendCameraShadowReports` statement) otherwise unchanged
    - Attach the condition to exactly three resources: `(cameraShadowRuleRole.node.defaultChild as iam.CfnRole).cfnOptions.condition`, `(cameraShadowRuleRole.node.findChild('DefaultPolicy').node.defaultChild as iam.CfnPolicy).cfnOptions.condition`, and `cameraRegistryShadowRule.cfnOptions.condition`
    - The `CameraShadowReportQueueArn` CfnOutput stays unconditional (its value is a plain derived string); note `portalAccountId` is the already-computed `props.portalAccountId || cdk.Stack.of(this).account`, so the same-account default correctly evaluates the condition false
    - _Bug_Condition: isBugCondition_Gap2 secondary manifestation (Req 1.5) — deploying the unconditional fixed names into an account that already has the rule/role fails on create (from design)_
    - _Expected_Behavior: Property 4 — the three resources carry a condition equivalent to Not(Equals(portalAccountId, AWS::AccountId)); created exactly when the use-case account differs from the portal account (from design)_
    - _Preservation: Property 6 — cross-account synth resource properties identical, differing only by the condition wiring and the version bump; DDAPortalUseCaseAccountStack security IAM baselines untouched (from design)_
    - _Requirements: 2.5, 3.3_

  - [x] 3.7 Add the fix-checking jest assertions to `edge-cv-portal/infrastructure/test/camera-shadow-sync-provisioning.test.ts`
    - **Property 3** (fix checking, on the synthesized ComputeStack template): exactly one IoT topic rule whose SQL is exactly `SELECT *, topic(3) AS thing_name FROM '$aws/things/+/shadow/name/dda-camera-registry/update/documents'`, `awsIotSqlVersion` `2016-03-23`, enabled (`ruleDisabled: false`), single action delivering to the `dda-portal-camera-shadow-reports` queue with `useBase64: false`, through a role assumable by `iot.amazonaws.com` whose policy allows `sqs:SendMessage` scoped to that queue's ARN
    - **Property 4** (fix checking): the new ComputeStack rule role carries NO `RoleName` property (CDK-generated) and the rule is named `dda_camera_registry_shadow_documents_portal`; in the UseCaseAccountStack template, the condition expression is `Fn::Not[Fn::Equals[portalAccountId, Ref AWS::AccountId]]` and exactly the role, its default policy, and the topic rule carry it; `STACK_VERSION` output is 1.6.0
    - **Property 6** (preservation, extends the task 2 describe): the cross-account usecase template's `CameraRegistryShadowRule`/`DDACameraShadowRuleRole` properties equal their pre-fix values (the existing `camera-registry-infra.test.ts` assertions continue to pass verbatim); `UserAccountsShadowRule` and the queue/DLQ/queue-policy unchanged
    - Note: Properties 3, 4, 6 are checked by jest on synthesized templates — the "for any synthesized template" quantification is discharged by synthesis determinism, matching the repo's established practice for CDK properties; tag each describe/test with `**Feature: camera-shadow-sync-provisioning, Property N: {property_text}**`
    - Run `npx jest test/camera-shadow-sync-provisioning.test.ts` and `npx tsc --noEmit` in `edge-cv-portal/infrastructure` — fix-checking and preservation describes pass; exploration describe now passes too (verified formally at 3.9)
    - _Requirements: 2.4, 2.5, 2.6, 3.3, 3.4_

  - [x] 3.8 Regenerate the ComputeStack IAM baseline pair symmetrically (D10)
    - Recapture `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json` from a live `cdk synth` of the fixed tree; mirror the identical new resources/statements (the `CameraShadowRuleRole` role, its `DefaultPolicy` carrying the `SendCameraShadowReports` statement, and the topic rule) into `iam_baseline_EdgeCVPortalComputeStack.unfixed.template.json`, so the statement-multiset symmetric difference between the two files remains exactly the recorded I1–I4 set (established practice: integration commits `2308311`, `bb2b9cc`)
    - `iam_baseline_cdk_i_changes.json`, every security test file, and the `DDAPortalUseCaseAccountStack` baselines are untouched (D7: the CfnCondition does not alter any synthesized IAM statement)
    - Run the security IAM CDK-synth tests and confirm `test_baseline_drift_confined_to_I1_I4` and `test_synth_iam_statements_match_fixed_baseline` are green
    - _Preservation: Property 6 — the security IAM baseline tests pass with the symmetric regeneration (from design)_
    - _Requirements: 3.7_

  - [x] 3.9 Verify the bug condition exploration tests now pass
    - **Property 1: Expected Behavior** - Gap 1: provisioning yields an HTTPS-compatible shadow grant; **Property 2: Expected Behavior** - Gap 1: the ensure step is idempotent; **Property 3: Expected Behavior** - Gap 2: ComputeStack provisions the camera shadow topic rule; **Property 4: Expected Behavior** - Gap 2: no fixed-name collisions between the two definitions
    - **IMPORTANT**: Re-run the SAME tests from task 1 — do NOT write new tests
    - The tests from task 1 encode the expected behavior; when they pass they confirm both gaps are fixed
    - Run `test_gap1_exploration.py` and the jest exploration describe from task 1
    - **EXPECTED OUTCOME**: Tests PASS (the ensure block's written statement is variable-free; the idempotency check rejects the variable-only document; the shadow grant is provisioned at the IoT policy layer; the ComputeStack template carries the camera-registry rule; the usecase fixed-name resources carry the cross-account condition)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

  - [x] 3.10 Verify preservation tests still pass
    - **Property 5: Preservation** - setup_station.sh outside the fix sites, and the golden; **Property 6: Preservation** - infrastructure unchanged outside the additions; **Property 7: Preservation** - edge and backend code untouched
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests
    - Run the task 2 tests: `test_setup_station_preservation.py` content anchors, the jest preservation describe, and `test_preservation_dependency_setup_station.py` against the recaptured golden
    - Confirm via the diff that no file under `src/backend/camera_sync/` was touched and `camera_registry.py`/`camera_sync.py` are unchanged (Property 7)
    - **EXPECTED OUTCOME**: Tests PASS (no regressions: script anchors byte-identical; cross-account usecase properties and ComputeStack siblings unchanged; golden enforced with the test file untouched)
    - Confirm all tests still pass after the fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.8_

- [x] 4. Checkpoint - Ensure all tests pass
  - Infrastructure: `npx jest` in `edge-cv-portal/infrastructure` (existing 30 tests plus the new `camera-shadow-sync-provisioning.test.ts` suite, all passing) and `npx tsc --noEmit` clean (Req 3.7)
  - Security preservation suite (`test/backend-test/security/`): setup-station golden and both IAM CDK-synth layers pass with the rebaselines from 3.4 and 3.8 (Req 3.7, 3.8)
  - Portal backend suite (`edge-cv-portal/backend/tests`): 883 passing baseline, including `test_camera_shadow_sync_integration.py` (rule SQL contract unchanged by design) (Req 3.9)
  - LocalServer suite: `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test` — 204 passed + 3 skipped baseline, plus the new `camera_shadow_sync` tests (exploration, preservation, and Hypothesis property suites)
  - Ensure all tests pass, ask the user if questions arise

- [x] 5. Migration cleanup in account 164152369890 (REQUIRES USER COORDINATION — do not start without explicit go-ahead)
  - **NOTE**: This task deletes live cloud resources and depends on the first ComputeStack deploy containing this fix having completed; it is user-coordinated, not automated
  - After the fixed ComputeStack deploy (which creates `dda_camera_registry_shadow_documents_portal`), the manually created resources are superseded — delete them:
    - `aws iot delete-topic-rule --rule-name dda_camera_registry_shadow_documents`
    - `aws iam delete-role-policy --role-name DDACameraShadowRuleRole --policy-name SendCameraShadowReports`
    - `aws iam delete-role --role-name DDACameraShadowRuleRole`
  - Delete-after-deploy is recommended (no ingest gap); the window where both rules forward the same shadow events is safe because the `camera_sync.py` reduce_report reducer is idempotent under duplicate delivery (camera-registry-sync design). Delete-before-deploy is also acceptable (short ingest gap, no duplicates)
  - The manually created IoT policy version 3 needs NO migration: the fixed ensure step's predicate recognizes it and makes no write (Property 2)
  - Optional operator verification (design Integration Tests): run the fixed `setup_station.sh` on a fresh device — observe exactly one new policy version created (and none on re-run), HTTP 200s replacing 403s in greengrass.log, and portal camera sync flowing without manual refresh
  - _Requirements: 2.4, 2.5, 2.6_

## Notes

- **Test-first ordering is mandatory**: task 1 (bug conditions) must FAIL and task 2 (preservation) must PASS on the UNFIXED code before implementing task 3. Do not modify `setup_station.sh`, `compute-stack.ts`, or `usecase-account-stack.ts` until the tests are written and their expected outcomes documented.
- **Property references**: Properties 1–2 (Bug Condition/fix, Gap 1) validate Requirements 2.1–2.3; Properties 3–4 (Bug Condition/fix, Gap 2) validate 2.4–2.6; Properties 5–7 (Preservation) validate 3.1+3.2+3.8, 3.3+3.4+3.7, and 3.5+3.6+3.9 respectively, per the design's Correctness Properties.
- **Confirmed root causes (file/line evidence)**: HTTPS-incompatible heredoc resource `arn:aws:iot:*:*:thing/${iot:Connection.Thing.ThingName}` and false-idempotent `grep -q "iot:UpdateThingShadow"` in the thing-policy ensure block (`setup_station.sh` ~1101–1195, grep at ~1146); wrong-layer `ShadowManagerSyncPolicy` IAM policy in step 3.6 (~1542–1574); camera-registry topic rule defined only in `usecase-account-stack.ts` (~792) with fixed names, absent from `compute-stack.ts` (Gap 2). Both gaps confirmed empirically: 403s in greengrass.log reproduced with raw curl on device `ryanorinagxdevkithomelabjp622`; no `dda_camera_registry_shadow_documents` rule in account 164152369890 while `dda_user_accounts_shadow_documents` existed.
- **Design decisions in force**: append-not-replace from the live default version (D2); pure-Python 3.6 stdlib helper as the property-tested seam (D3); `GreengrassV2IoTThingPolicy` name first with cert-principal discovery fallback (D4); keep the bash 5-version pruning (D5); CDK-generated role name + `_portal` rule-name suffix in ComputeStack (D6); deploy-time `CfnCondition` — NOT a synth-time `if`, which would break `test_preservation_iam_cdk_synth.py`'s same-account fixture (D7); step 3.6 `put-role-policy` kept byte-identical, prose-only edits (D8); `STACK_VERSION` 1.6.0 (D9); symmetric IAM baseline regeneration preserving the I1–I4 symmetric difference (D10).
- **Security preservation gate**: `setup_station.sh` is pinned whole-file by the setup-station golden (rebaseline in 3.4, test file untouched); `compute-stack.ts` synth is pinned by the `EdgeCVPortalComputeStack` IAM baseline pair (symmetric regeneration in 3.8); the `DDAPortalUseCaseAccountStack` baselines and `iam_baseline_cdk_i_changes.json` need no change.
- **Test baselines**: infrastructure jest 30 passing + `npx tsc --noEmit` clean; portal backend 883 passing; LocalServer `test/backend-test` 204 passed + 3 skipped (`PYTHONPATH=src/backend:test/backend-test`). All grow only by the new tests.
- **PBT conventions**: Hypothesis properties run with minimum 100 iterations and are tagged `**Feature: camera-shadow-sync-provisioning, Property N: {property_text}**`; infrastructure Properties 3, 4, 6 are discharged by jest on deterministic synthesized templates per repo practice.
- **Migration is user-gated**: task 5 deletes live cloud resources in account 164152369890 and requires the fixed ComputeStack to be deployed first; it runs only with the user's explicit go-ahead and coordination. The manual IoT policy version 3 needs no migration (the predicate recognizes it).
- **On-hardware fresh-provisioning verification is operator-gated**: already verified end to end on the production account via the manual fix this design mirrors; a fresh `setup_station.sh` run on a new device (one policy version created, none on re-run, 200s in greengrass.log) is documented in task 5 as optional operator verification, not automated.
