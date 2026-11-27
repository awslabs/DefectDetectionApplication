# Verification notes: car-assembly-check

Evidence for the USER tasks of `tasks.md`, filed under the section names the tasks give. Task 23.1 completes this file. Times are UTC.

## Portal deploy #1

Task 18.2, both floors at their defaults. Evidence files named in this section by a relative path are in the run directory `/home/ubuntu/github/jp5/main/.agents/tasks/DefectDetectionApplication-live-car-assembly-check-18-19-2026-11-27/`. Its `live-log.md` lists every AWS call and every Portal call.

### What was deployed

- **Where and when:** the demo Portal (account 326334468650, us-west-2, single-account topology), 2026-11-27 21:26:47–21:50:36Z.
- **`<DEPLOY_COMMIT>`:** `16f3e8ab391a3d184530c8ae9fb01057b6fe6f04`, `main` at the PR #174 merge, which is also `<MERGE_COMMIT>`. `origin/main` was fetched again at 19:46:15Z and had not moved. The deploy ran in the detached worktree `.worktrees/car-assembly-check-deploy-16f3e8ab`, whose `git status --porcelain` was empty before and after.
- **Separately deployed commits it carries** (18.1 (b)(2); both are ancestors of `16f3e8ab`):
  - `e7547824` (PR #172), the Grounded-SAM worker image, deployed by change set at 02:33Z;
  - `d1d94f72` (PR #173), the auto-label time limits, deployed by change sets at 15:58–16:03Z and the frontend at 16:09Z.
  It also carries `c066b4c8` (PR #164, rfdetr-seg-support) and `268dc5f5` (PR #163). Before this deploy, the stacks ran the full deploy of `36934d37` (2026-11-25) plus those change sets, and BuildFleetStack ran `418b1faf`.
- **Task text followed:** 18.2 as corrected by spec-review NIT 2 (commit `a6b211e2`). The deploy went through `./deploy-infrastructure.sh` and then `./deploy-frontend.sh`, never a bare `cdk deploy`. `npx cdk diff --all` ran first with the context the scripts resolve, and every entry was classified against the corrected expected list.
- **Owner decisions applied** (relayed by the parent session):
  - D1, template-only diff;
  - D2, no deployment request to `mic-730ai`;
  - D3, the check workflows are deleted after the readings;
  - D6, the Portal checks run through the API, signed in as one temporary PortalAdmin (method A);
  - the served `config.json` is copied before `deploy-frontend.sh`;
  - the go for the deploy, including O1 and O2 below.

### Pre-deploy checks

`state-18-2/precheck-before-deploy/summary.txt`, read 21:25:37–21:26:21Z, right before the deploy:

- Only the 22 known nested stacks in `REVIEW_IN_PROGRESS` were in progress (created 2026-10-22 and 10-28 under parent stacks deleted then).
- The 12 top-level stacks were `UPDATE_COMPLETE`, none updated after 16:18:49Z, none with a change set.
- None of the 105 functions was modified after 16:03:11Z.
- All 13 `dda-portal-build-jobs` items were terminal, so no component build or JP5 Fleet job was running.
- `/dda-portal/layout-check-min-local-server-versions` and `/dda-portal/rfdetr-seg-min-local-server-versions` both returned `ParameterNotFound`.
- `LAYOUT_CHECK_FLOOR` and `RFDETR_SEG_FLOOR` were unset in the deploying shell, and no floor `-c` was passed.
- No deploy or build process ran on the host. Another session's local image build (`docker build` of `dda-workflow-test-sandbox:triton-copies-resync-branch`, no push and no AWS call) was listed and not treated as a stop, as the parent session confirmed.
- The parent session confirmed that no other run was deploying to the demo Portal and that no training launch was in progress. Labeling job `car-assembly-instances-r2` was Completed (70 of 70), and `r3` was not created (owner decision), so no auto-label ran.
- `deploy-frontend.sh` Step 6's trusted UseCase account list resolved to `326334468650` from SSM.

### Diff and classification

`npx cdk diff --all --no-change-set --profile dda-usw2-proc -c cloudFrontDomain=d23jpvi9ax40sj.cloudfront.net -c portalRegistryEnforced=false -c buildFleetNatGateway=true -c sharedAuthorizerId=ckc9gq`, run 19:48:40–19:49:56Z with the context resolved again at 19:46:59Z (identical at 21:26:20Z). It created no change set. The CLI ignores `--all` for `diff` and diffs every stack anyway. Full classification: `18.2-cdk-diff.classified.md`.

10 stacks had differences, with 91 entries: ComputeStack 48 and its ApiGateway nested stack 1, NodeDesigner 10, Telemetry 9, WorkflowDebug 8, BuildFleet 5, Assistant 4, TestRunner 2, SyntheticData 2, Mcp 2. Auth, Storage, Frontend and the other 7 nested stacks had none.

| | expected entry (18.2 as corrected) | result |
|---|---|---|
| E1 | a new code asset on every function built from `backend/functions` | 69 functions: 68 to asset `a45d53ba…`, `TrainingHandler` to its bundle `629bc840…`; the ApiGateway nested stack's `TemplateURL` |
| E2 | new versions of the changed layers | 13 replacements: the 5 `workflow_core` layers, the 7 shared layers (pending since `36934d37`; files unchanged), `AssistantGuideLayer` (guide pages 04, 08, 13) |
| E3 | first full deploy since `36934d37`: the `SageMakerEventBridgeIntegration` re-run, `MCP_BUILD_SHA`, WorkflowDebug and Telemetry versions and aliases | `Timestamp` changed; `MCP_BUILD_SHA` `36934d37e84a` to `16f3e8ab391a`; one ingest Lambda version added and one removed per stack, `IngestAlias` moved |
| E4 | no change to `EdgeCVPortalBuildFleetStack`'s NAT gateway or routes | code assets of its 5 functions only |
| E5 | code and layer updates of `workflow_packaging.py`, `deployments.py`, the `workflow_core` layer and the frontend bundle; `captures.py` only if group 11 is included | as listed; `captures.py` identical (group 11 not included) |
| E6 | `WORKFLOW_LAYOUT_CHECK_MIN_LOCAL_SERVER_VERSIONS={}` added on `DeploymentsHandler` and `WorkflowPackagingHandler` | added on exactly these two |
| E7 | `WORKFLOW_RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS` unchanged | no entry; the synthesized value equals the committed literal and the deployed value |
| E8 | no new route, Lambda or IAM statement | the only added resources are the 2 Lambda versions of E3; no IAM section |
| E9 | the test-runner stack's sandbox image reference unchanged | TestRunner changes `TestRunStepsHandler`'s code and its `workflow_core` layer only |
| O1 | outside the list | `QuickSetupBundle` (`Custom::CDKBucketDeployment`) `SourceObjectKeys`: the same 5 files; the asset hash includes the tree's absolute path through the bundling options |
| O2 | outside the list | the description of the Telemetry stack's output `TelemetryDataPlaneGrant` (`?12.3` deployed, `§12.3` synthesized); not printed by the CLI ("mangled non-ASCII") |

O1 and O2 were in the PR #173 diff too. They stopped the deploy until the owner's go, which included them. A leaf-by-leaf comparison of every synthesized template with the deployed one found no other change. The deployed functions and layers ran exactly the old assets the diff named. The one container image asset (`8255b4bf…`, the Grounded-SAM worker) was already in ECR, so nothing was built.

### Deploy

`scripts/18-2-run.sh`, detached from the terminal with stdin from `/dev/null` (`state-18-2/run.log`):

- 21:26:47–21:45:54Z (1147 s): `./deploy-infrastructure.sh`, rc 0. It passed the context above and ran `npx cdk deploy --all --require-approval never --force`. All 12 stacks deployed; Auth, Storage and Frontend had no changes.
- 21:45:59Z: the served `config.json` copied to `frontend/public/config.json` (sha256 `a7baf3d3…`, the 6 keys).
- 21:46:11–21:50:36Z (265 s): `./deploy-frontend.sh`, rc 0.
  - Steps 1–5 built the bundle `index-DdBY7aGs.js` (build info: commit `16f3e8ab`, 21:47:51Z) and synced `dist/` with `--delete`: the new bundle was uploaded, and `index-B4vs8VT6.js` and its map were deleted. They then uploaded `index.html` and `config.json` and invalidated `/*` (`ID7PJHYY9BQIXIZ4VQ4M67JQ7Z`).
  - Step 6 ran `cdk deploy EdgeCVPortalComputeStack` with `-c trustedUseCaseAccountIds=326334468650`: Auth, Storage and TestRunner had no changes, and ComputeStack changed only the custom resource `Timestamp`.
- Neither log has a prompt or a failure line.

Change-set summary per stack (each change set recorded while it executed; `state-18-2/changesets/`). "Static" counts the resources whose own template values change. "Dynamic only" counts the resources CloudFormation lists because a reference they read is re-evaluated; they were updated in place or not at all.

| stack | change set (created) | changes | Static | Dynamic only | added / removed | replaced |
|---|---|---|---|---|---|---|
| TestRunner | 21:29:15Z | 4 | 2: 1 function, 1 layer | 2 | – | `TestRunnerWorkflowCoreLayer` |
| Compute | 21:30:17Z | 74 | 48: 43 functions, 2 layers, `QuickSetupBundle`, `SageMakerEventBridgeIntegration`, the ApiGateway nested stack | 26 | – | `SharedLayer`, `WorkflowCoreLayer` |
| WorkflowDebug | 21:37:53Z | 24 | 6: 3 functions, 2 layers, `IngestAlias` | 16 | 1 / 1 Lambda version | 2 layers |
| SyntheticData | 21:38:50Z | 24 | 2: 1 function, 1 layer | 22 | – | `SyntheticSharedLayer` |
| BuildFleet | 21:39:41Z | 44 | 5: 5 functions | 39 | – | none |
| Telemetry | 21:40:33Z | 47 | 7: 4 functions, 2 layers, `IngestAlias` | 38 | 1 / 1 Lambda version | 2 layers |
| NodeDesigner | 21:41:41Z | 19 | 10: 8 functions, 2 layers | 9 | – | 2 layers |
| Mcp | 21:43:56Z | 13 | 2: 1 function, 1 layer | 11 | – | `McpSharedLayer` |
| Assistant | 21:44:42Z | 38 | 4: 2 functions, 2 layers | 34 | – | `AssistantSharedLayer`, `AssistantGuideLayer` |
| Compute (Step 6) | 21:49:34Z | 1 | 1: `SageMakerEventBridgeIntegration` | 0 | – | none |
| Auth, Storage, Frontend | – | no changes | | | | |

Outcome: of 1,721 resources in the 20 live stacks, 14 got a new physical id: the 13 layer versions and the `SageMakerEventBridgeIntegration` custom resource. Nothing in a nested stack was replaced. The nested change sets list their methods and permissions as possible replacements only because their parameters were known after apply, and the nested stacks' events show no resource change except `ComponentConfigurationHandler`'s code. The Build Fleet NAT gateway, EIP and routes (8 resources) keep their ids and were not updated. All 20 stacks ended `UPDATE_COMPLETE`, with no rollback and no change set left.

### Post-deploy readings

`aws lambda get-function-configuration` on both Lambdas and `aws lambda list-functions`, 21:51–21:53Z (`state-18-2/postcheck/summary.txt`):

- (i) `WORKFLOW_RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS` on `DeploymentsHandler` (`EdgeCVPortalComputeStack-DeploymentsHandler70E83D8-mKnssydwOEcW`) and `WorkflowPackagingHandler` (`EdgeCVPortalComputeStack-WorkflowPackagingHandler7-vWH7rI8irvFo`) is `{"arm64_jp5":"1.0.62","arm64_jp6":"1.0.85","x86_64":"1.0.56","x86_64_nvidia":"1.0.56"}`, byte for byte the committed literal of `16f3e8ab`. It names `arm64_jp5` with `1.0.62`, the value that refuses `mic-730ai` until 19.3.
- (ii) `WORKFLOW_LAYOUT_CHECK_MIN_LOCAL_SERVER_VERSIONS` is `{}` on both (LastModified 21:33:58Z and 21:33:59Z).
- (iii) No other function carries either variable.

Further readings:

- `MCP_BUILD_SHA=16f3e8ab391a`.
- The rule `sagemaker-eventbridge-enabler` is absent, as before.
- 69 functions run new code. Their code and the 13 new layer versions equal the `16f3e8ab` tree file for file: the functions asset (175 files), the `TrainingHandler` bundle (175 files plus 8 from `datasets/`), the shared layer (2,317 files), the `workflow_core` layer (153 files) and the guide layer (26 files).
- The only environment variable names added are the two Layout floor variables.
- Stack output values are unchanged.
- The served bundle carries the inline V14/W4 checks of `inlineChecks.ts` (`V14_LAYOUT_CONFIG_INVALID`, `W4_LAYOUT_NO_DETECTOR`, `layout_check`), which the old bundle did not. The served `config.json` is unchanged (sha256 `a7baf3d3…`).

### Portal checks

`scripts/portal_checks_temp_admin.py`, 2026-11-27 22:48:38–22:49:33Z, through the deployed API. Records: `state-18-2/portal-checks/`.

**Temporary PortalAdmin** (method A, the owner's choice; the procedure of the PR #173 lab check):

- **Account:** `tmp-cac-182-check-20261127T224838Z`. It was created at 22:48:43Z with `AdminCreateUser` and `MessageAction SUPPRESS`, so no email was sent. It had the pool's required attributes, with the address `<username>@example.com`, and `custom:role=PortalAdmin`. It was in no group; the pool has none.
- **Registry row:** its one global row (`PortalAdmin`, `enabled`), written at 22:48:49Z.
- **Password:** generated inside the check process, set with `AdminSetUserPassword` (permanent) and passed to the sign-in as a function argument. It never appeared in a command line, an environment variable, a file or a log. A scan of the 19 records found neither the password nor the ID token.
- **Sign-in:** at 22:48:52Z through the Portal web client, with USER_SRP_AUTH. The pool has no Lambda trigger, so signing in wrote nothing on the Portal side.
- **Deletion:** at 22:49:15Z, the registry row and then the Cognito user. Read back: `UserNotFoundException`, and no registry row for its sub or its username. The registry holds 2 rows, as before.
- **What stays:**
  - The 4 audit entries the checks made under the account (`create_workflow` and `delete_workflow` for each workflow).
  - Three alerts. The rule `dda-portal-cognito-admin-activity` fires on any pool change made outside the User Manager, and it fired 3 times in 22:45–22:50Z, once each for `AdminCreateUser`, `AdminSetUserPassword` and `AdminDeleteUser` (CloudWatch `TriggeredRules` and `Invocations` 3, no failed invocation). Its topic `dda-portal-cognito-admin-alerts` has no subscription, so no message reached anyone. The rule's 3 triggers in 22:40–22:45Z are not from this run.

**Readings:**

- **Palette:** `GET /workflows/node-catalog` lists `layout_check` ("Layout Check") under post-processing with four parameters, mapped to the executor binding `layout_check` on every architecture:
  - `expected_regions`: string, required, multiline;
  - `min_iou`: 0.7, range 0.01–1;
  - `min_confidence`: 0.5, range 0–1;
  - `min_outline_overlap`: 0.1, range 0–0.5.

  18.2 said "three parameters" because it predates O19, which added `min_outline_overlap`; its text is corrected in the same commit as this section. No browser was used. The served bundle carries the inline V14/W4 checks (see the readings above).
- **V14/W4 findings on a saved draft:** the workflow `folder_source → bedrock_inference → layout_check → mqtt_publish` was saved with a malformed `expected_regions` (`{"part_a": [[0.1, 0.1], [1.4, 0.1]]}`). `POST /workflows` gave 201, and `POST /workflows/{id}/validate` gave 200 with `passed: false` and two findings:
  - error `V14_LAYOUT_CONFIG_INVALID`: "Node 'n3': parameter 'expected_regions': Region 'part_a': point 2 x coordinate must be between 0 and 1.";
  - warning `W4_LAYOUT_NO_DETECTOR`.

  `bedrock_inference` supplies the inference input because the demo account has no registered model, so W4 is expected for this graph.
- **Refusal, the first half of Requirement 10.2(d):** the same graph with a valid `expected_regions` validated with `passed: true` and only the W4 warning. `POST /workflows/{id}/package` for all six architectures in one request gave **409 `LAYOUT_CHECK_UNSUPPORTED_ARCH`**, with one finding per architecture. Each finding names the node type and the architecture, for example: "Architecture 'arm64_jp5' has no LocalServer version known to support layout_check nodes, so node(s) n3 cannot be packaged for it. Architectures with a supporting LocalServer version: none". Nothing was registered: there is no `dda.workflow.<id>` component version and no object under `workflows/components/<id>/`.
- **Clean-up (D3):** both workflows were deleted (200) and read back 404. Their version items and documents are gone. A final listing found no `cac-182-check-*` workflow among the use case's 6.
- **A Seg workflow for `arm64_jp5` still packages (201): not run**, by the owner's decision. The demo account has no registered model, so this stays open until group 20's model is registered. Reading (i) shows that the deployed Seg floor still covers `arm64_jp5`.
- **Its deployment to `mic-730ai` is refused with 409 `INCOMPATIBLE_LOCAL_SERVER`: not sent** (D2; the device is shared and gate 0.15 is open). Open.
- **The Results tab of an existing capture set renders unchanged:** no capture set exists in the demo account (its inference-results bucket holds only `workflow-tuning/`), so nothing can render. `ResultsTab.tsx`, `ResultsViewer.tsx`, `services/api.ts` and `captures.py` are unchanged between the deployed `d1d94f72` and `16f3e8ab`.
- **An earlier run at 21:22Z,** by the owner's script from before the switch to method A, reached the Portal before the deploy. The catalog had no `layout_check`, and both validations answered `UNKNOWN_NODE_TYPE`. Its two workflows were deleted and leave nothing behind. Its records are kept apart in `state-18-2/portal-checks-run1-predeploy/` and are not post-deploy evidence.

### Notice

The tasks ask that four parties be told the demo Portal is from now on deployed from `main` at or after `16f3e8ab`: the rfdetr-seg-support session, the `main` owners, and the owners of the two separate workflows (the worker fix, PR #172, and the time-limit change, PR #173). The notice was handed to the parent session to relay, with this step's completion message.

### Stack outputs after the deploy

No output value of any stack changed. Every output of every stack, before and after, is in `state-18-2/snapshot-before/stacks.json` and `state-18-2/snapshot-after/stacks.json`. The outputs later tasks use:

- ComputeStack:
  - `ApiUrl` `https://orglxj6a3h.execute-api.us-west-2.amazonaws.com/v1/`;
  - `RfDetrSegEnabled` `true`;
  - `SageMakerEventBridgeStatus` "SageMaker EventBridge integration enabled successfully".
- FrontendStack: `DistributionDomainName` `d23jpvi9ax40sj.cloudfront.net`.
- TestRunnerStack: `TestSandboxRepositoryUri`, the `dda-workflow-test-sandbox` repository (18.3).
- BuildFleetStack:
  - `BuildJobsTableName` `dda-portal-build-jobs` and `BuildLogGroupName` `/dda/portal-builds` (19.1);
  - `BuildNatGatewayId`, unchanged, so the NAT gateway stays.
- TelemetryStack: `TelemetryDataPlaneGrant` `absent`.
- AuthStack, AssistantStack and McpStack: the user pool, its web client and the assistant and MCP endpoints are unchanged.

### Undo

There is no one-command rollback. The 'before' snapshot (`state-18-2/snapshot-before/`; the templates and resource lists are under `/home/ubuntu/dda-tmp/car-assembly-live-18-19/snapshot-before/`) is the basis for change sets that would restore the previous templates, and the old asset zips are still in the bootstrap bucket. That needs its own go.
