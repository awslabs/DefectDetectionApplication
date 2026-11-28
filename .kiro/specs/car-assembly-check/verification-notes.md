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

## Sandbox image

Task 18.3: the cloud test sandbox image is built from `<DEPLOY_COMMIT>` and pushed to the demo account. As in "Portal deploy #1", a relative evidence path is in the run directory, and `live-log.md` lists every command.

### Result

- **Repository:** `326334468650.dkr.ecr.us-west-2.amazonaws.com/dda-workflow-test-sandbox`, the repository of the demo account's test-runner stack (326334468650, us-west-2).
- **Digest** (`<OWNER: image digest>`): `sha256:0a8177eaa723ebb2df3e141c56e03341737c6287746d2b7a0acab1400ad10924`. Its local image id is `sha256:4319a8415d7896d7bde5e7e3efe952b2aa04df9e8016ca521fe251d6db9b0a03`.
- **Tags:** `main-16f3e8ab`, pushed at 2026-11-27 23:21:56Z, and `latest`, set by the server-side retag at 23:22:00Z.
- **Source commit:** `16f3e8ab391a3d184530c8ae9fb01057b6fe6f04` (`<DEPLOY_COMMIT>`, 18.1), in the detached worktree `.worktrees/car-assembly-check-sandbox-16f3e8ab`. Its `git status --porcelain` was empty before and after the build.
- **Date:** 2026-11-27. Built 23:11:13–23:11:17Z, pushed and retagged 23:21:47–23:22:03Z.
- **The tag the Portal pulls:** `latest`. At `16f3e8ab`, neither `cdk.json` nor the deploy scripts set `testSandboxImageTag`, and the code default is `latest` (`test-runner-stack.ts:75`, `node-designer-stack.ts:1177`). Both deployed task definitions pull `…/dda-workflow-test-sandbox:latest` on X86_64: `EdgeCVPortalTestRunnerStackTestSandboxTaskDefinition44315046:2` (workflow test runs) and `EdgeCVPortalNodeDesignerStackSimulatorTaskDefinitionFAE45D8A:1` (plugin simulations). No CDK deploy was needed.

### Inputs at `16f3e8ab`, re-read

- **`test-sandbox/Dockerfile`:** `COPY … workflow_core` at `:218`, `COPY … harness` at `:221`, `ENV PYTHONPATH=/opt/harness` at `:223` and `CMD ["/opt/dda/harness-venv/bin/python", "-m", "harness"]` at `:228`. These are the same lines as at `268dc5f5` and `e7547824`.
- **`harness/bindings.py`:** `:285-288` define `LAYOUT_CHECK_BINDING = "layout_check"` and add it to `SCENE_ANALYTICS_BINDINGS`.
- **`test-sandbox/README.md`:** "Build and push" is at `:186-253` and "Build gotchas" at `:255`.
- **What changed since the previous image's source `e7547824`:** the image inputs differ only in five `workflow_core` files and in `harness/bindings.py`:
  - `analytics/__init__.py`;
  - `analytics/layout.py`, which is new;
  - `catalog/nodes.py`;
  - `validator/__init__.py`;
  - `validator/checks.py`.

  Two other changed files are under `test-sandbox/tests/`, which the Dockerfile does not copy.
- **Staged, git-ignored:** the DDA plugin set (README "Staging the DDA plugins"), 58 files copied from the main checkout. Their sha256 equals the set the previous image was built with. `dda_triton_resources/` was used as committed.

### Build

- **Command,** from `edge-cv-portal/` of the build worktree (the task's command, plus `--progress=plain` for the log): `docker build --progress=plain --platform linux/amd64 -f test-sandbox/Dockerfile -t 326334468650.dkr.ecr.us-west-2.amazonaws.com/dda-workflow-test-sandbox:main-16f3e8ab .` -> rc 0.
- **Build steps:** steps 1–17 of 20 came from the local cache of the `e7547824` build. The two `COPY` steps for `workflow_core` and `harness` were built, and nothing was pulled.
- **Layers:** 18 of the 20 layers equal those of the previous image. Layer 18, `workflow_core`, is 657 kB, and layer 19, `harness`, is 172 kB.
- **Image:** linux/amd64, 7,678,708,940 bytes, `Cmd ["/opt/dda/harness-venv/bin/python","-m","harness"]`.
- **Deviation, the build host:** the task names a native x86_64 host. This build ran on the run's arm64 host under qemu, as the owner decided (plan D4 (a)). The previous image, which passed a Portal test run on Fargate, was built the same way.
- **Deviation, no prune:** no `docker system prune` ran. It would have removed other sessions' images and cache, and 1.8 TB was free.

### Checks before pushing

Each check ran as `docker run --rm --platform linux/amd64 --pull=never --network none … main-16f3e8ab`, and all exited 0:

| Check | Output |
|---|---|
| README step 2: `--entrypoint python3 … -c "import sklearn; print('sklearn', sklearn.__version__)"` | `sklearn 1.3.2` |
| README step 2: `--entrypoint /opt/dda/harness-venv/bin/python … -c "import sys, PIL; print('harness', sys.version.split()[0], 'Pillow', PIL.__version__)"` | `harness 3.11.18 Pillow 13.0.0` |
| The task's check, through the interpreter the `CMD` uses: `--entrypoint /opt/dda/harness-venv/bin/python … -c "from workflow_core.analytics import layout; from harness import bindings; assert 'layout_check' in bindings.SCENE_ANALYTICS_BINDINGS; print('layout_check ok')"` | `layout_check ok` |

### Push and retag

These ran on the owner's go, relayed by the parent session.

- **Right before the push:** `describe-images` showed one image, `sha256:3813e4ebdb57f890db64857348d355242febe1e3fc485b03e6c3710a64985d0d`, tagged `latest` and `main-e7547824`. That image was built from `e7547824` and pushed 02:18:02Z. So `main-16f3e8ab` did not exist, and `latest` was on `3813e4eb…`. Either finding the other way would have stopped the step. No test-runner execution and no sandbox task was running.
- **Login:** the ECR login token was kept in a temporary docker client config, which was logged out and deleted after the push. The shared docker client configs were not changed.
- **Push:** `docker push …:main-16f3e8ab` uploaded 2 layers, and the other 15 distinct layers already existed. The digest is `sha256:0a8177ea…`.
- **Read-back:** `batch-get-image` returned a single-platform `application/vnd.docker.distribution.manifest.v2+json` manifest. The sha256 of its bytes equals the digest, and its config is the local image id.
- **Retag:** `aws ecr put-image --image-tag latest --image-manifest file://…/manifest.json --image-digest sha256:0a8177ea…` (the README's server-side retag).
- **Verification:** `aws ecr describe-images --image-ids imageTag=latest` returns `sha256:0a8177eaa723ebb2df3e141c56e03341737c6287746d2b7a0acab1400ad10924`, tags `latest` and `main-16f3e8ab`. The previous image `3813e4eb…` keeps only the tag `main-e7547824`.

### Scan on push

The registry's scanning configuration is `BASIC` with no rules, and the repository has `scanOnPush: true`.

| Image | Scan status | Completed | CRITICAL | HIGH | MEDIUM | LOW | UNDEFINED |
|---|---|---|---|---|---|---|---|
| `0a8177ea…` (new, `latest`) | `COMPLETE` | 2026-11-27 23:23:08Z | 57 | 460 | 362 | 52 | 24 |
| `3813e4eb…` (previous) | `COMPLETE` | 2026-11-27 02:22:14Z | 57 | 452 | 318 | 36 | 24 |

All 887 findings of the previous image are in the new list. The 68 extra findings are on OS packages that come from the 18 unchanged layers:
- `binutils`, `libbinutils`, `libctf0` and `libctf-nobfd0` `2.38-4ubuntu2.12`, 16 findings each;
- `libtiff5` and `tiff` `4.3.0-6ubuntu0.13`, 2 findings each.

The two new layers hold only Python sources. The extra findings therefore come from the scanner's newer vulnerability data, not from this change. The findings are recorded and were not acted on (`state-18-3/scan/compare.txt`).

### Effect and undo

- **Effect:** every workflow test run and plugin simulation started after 23:22:00Z pulls this image. Its harness keeps `layout_check` bindings instead of dropping them, as the third check above shows. Whether a cloud test run produces the `layout` section is read in 20.3, which comes only after this task.
- **Undo** (not run; needs its own go): put `latest` back on the previous image through its remaining tag:
  ```bash
  M=$(aws ecr batch-get-image --profile dda-usw2 --region us-west-2 --repository-name dda-workflow-test-sandbox --image-ids imageTag=main-e7547824 --query 'images[0].imageManifest' --output text)
  aws ecr put-image --profile dda-usw2 --region us-west-2 --repository-name dda-workflow-test-sandbox --image-tag latest --image-manifest "$M" --image-digest sha256:3813e4ebdb57f890db64857348d355242febe1e3fc485b03e6c3710a64985d0d
  ```
- **Coordination:** another session has local, unpushed sandbox images built from a tree that predates PR #174. The owner will ask that session to rebuild from `main` at or after `16f3e8ab` before it moves `latest`. An image without PR #174 would drop the `layout_check` binding again.

## Build

Task 19.1: the demo Portal's Build Fleet builds `aws.edgeml.dda.LocalServer.arm64JP5` from `<DEPLOY_COMMIT>`, and its version is read from the job. As in the sections above, a relative evidence path is in the run directory, and `live-log.md` lists every command. The task was followed as written: the spec-review NIT fixes in `a6b211e2` do not change 19.1.

### Result

- **`<DEMO_JP5_VERSION>`:** `1.0.4`, that is `aws.edgeml.dda.LocalServer.arm64JP5 1.0.4` in account 326334468650, us-west-2. It is the job's `result.published_version`, read in the job detail (`GET /builds/45e87f6a-…`, 2026-11-28 00:10:43Z) and in the re-read `dda-portal-build-jobs` item (00:11:19Z). Nobody chose it: the Fleet published the highest version plus one patch (`Publishing version: 1.0.4`, over `1.0.3`).
- **Fleet job:** `45e87f6a-7110-4406-afef-b74c2f26f0a8` (request `aa7b9a40-16e6-4555-b638-3d7321dd207a`), JP5, terminal status `succeeded`.
- **Resolved commit:** `16f3e8ab391a3d184530c8ae9fb01057b6fe6f04`, equal to `<DEPLOY_COMMIT>`. It was read from the job's log stream, because the job detail's Resolved commit is empty (see "After the job").
- **Requester:** `tmp-cac-191-build-20261127T235814Z`, a temporary PortalAdmin (sub `08a11330-50f1-70e8-06e3-df3c1338cece`), created for the request and deleted after the job.
- **Times:** submitted 2026-11-27 23:58:35Z, started 23:58:36Z, ended 2026-11-28 00:09:59Z.
- **Published artifacts:**
  - the component version, created 00:09:56Z, `DEPLOYABLE`, platform linux/aarch64;
  - `326334468650.dkr.ecr.us-west-2.amazonaws.com/dda/flask-app:arm64JP5-1.0.4` (`sha256:67498dc7bfc28d0b2636ff90dca9756455865f8715b135a95b0d19452cd3de8c`, pushed 00:09:46Z);
  - `326334468650.dkr.ecr.us-west-2.amazonaws.com/dda/react-webapp:arm64JP5-1.0.4` (`sha256:ba1cca43e6d4d77e0bef54d90f8bace3f628c04ee97915396246ca7d7f2e897e`, pushed 00:09:51Z);
  - the scripts artifact `s3://dda-component-us-west-2-326334468650/aws.edgeml.dda.LocalServer.arm64JP5/1.0.4/aws.edgeml.dda.LocalServer.arm64JP5-aarch64.zip`.

### Before the request

The state was read at 23:36–23:42Z and again at 23:57:25–23:57:55Z (`state-19-1/precheck-before-request/summary.txt`). The build-jobs table was read once more at 23:58:33Z, three seconds before the request (`state-19-1/build/build-jobs-before-request.json`).

- **The JP5 items of `dda-portal-build-jobs`** (read-only `aws dynamodb scan --table-name dda-portal-build-jobs --profile dda-usw2 --region us-west-2`; 13 items in all):

  | `build_job_id` | `status` | `requested_by_username` | `config_snapshot.source_ref` | `result.published_version` | Ended |
  |---|---|---|---|---|---|
  | `78e19e25-d96c-49c3-849d-e0518ea8d7fb` | succeeded | `admin` | none (the default branch) | `1.0.0` | 2026-11-15 21:06:17Z |
  | `e51c6f13-ed50-4cb0-8474-3f475a8c1a99` | succeeded | `kiro-temp-zone3-buildjp5` | `b0fbc53e3534725f32a8d391430dabf0823a05c2` | `1.0.1` | 2026-11-16 02:55:15Z |
  | `2ab20b2b-6ac3-497d-8c5f-24ce5d4e6cdd` | failed | `kiro-temp-zone3-fubuild` | `ab1f574d74f63e0a4bd5e4365467a7c6841fd83e` | none | 2026-11-22 04:47:37Z |
  | `37569b67-decd-425b-80af-634cc5862dd1` | succeeded | `kiro-temp-rfdetr-seg-g2-demo` | `feature/rfdetr-seg-support` | `1.0.2` | 2026-11-24 05:02:18Z |
  | `02414266-05ca-4a09-8b50-65cb567cd486` | succeeded | `kiro-temp-z3resume-jp5` | `268dc5f593b146c79131ab55e6b42022ad14efd4` | `1.0.3` | 2026-11-25 20:49:47Z |

  No JP5 item was `queued`, `provisioning`, `building` or `publishing`. No job of another target was running, on the dedicated server or elsewhere. All five ran dedicated on `srv-a4eb137f-…`.
- **The lineage:** `aws greengrassv2 list-component-versions --arn arn:aws:greengrass:us-west-2:326334468650:components:aws.edgeml.dda.LocalServer.arm64JP5 --region us-west-2` gave `1.0.3`, `1.0.2`, `1.0.1`, `1.0.0`, unchanged since 18.1.
- **No Portal deploy running:**
  - no stack was in a `*_IN_PROGRESS` status, apart from the 22 old `REVIEW_IN_PROGRESS` nested stacks;
  - the 12 top-level stacks were `*_COMPLETE`, none updated after the 18.2 deploy (the newest at 21:50:13Z) and none with a change set;
  - none of the 105 functions was modified after 21:51:16Z;
  - no deploy or build process ran on this host.

  The parent session confirmed for its runs. The data-training run's training launches do not conflict with the build.
- **The build server:** `srv-a4eb137f-3a13-4196-9ad0-991be921d9e1` (`jp5/6 build`, Ubuntu 22.04, arm64, m6g.4xlarge) was `running` with no `running_build_job_id`. Its instance `i-0758e9dd583c81d90` was `running`, with no SSM command pending or in progress.

### The request

- **Owner decisions:**
  - D5 (a): the dedicated server `jp5/6 build`, which every earlier JP5 job of this lineage used;
  - D6 (a): one temporary PortalAdmin (method A, as in "Portal deploy #1").
- **Deviation, the API instead of the Build page:** the request is the call the Build page makes. It was sent once through the Portal API, signed in as the temporary PortalAdmin through the web client (USER_SRP_AUTH): `POST /builds` with `{"targets": ["JP5"], "execution_mode": "dedicated", "server_id": "srv-a4eb137f-3a13-4196-9ad0-991be921d9e1", "repository": "https://github.com/awslabs/DefectDetectionApplication", "source_ref": "16f3e8ab391a3d184530c8ae9fb01057b6fe6f04"}` returned 201 at 23:58:36Z. The ref is the 40-character SHA, not a branch name.
- **The job as created:** `queued`, `required_arch` arm64, `config_snapshot.repository` and `config_snapshot.source_ref` as sent, `max_runtime_hours` 8.
- **The temporary user:**
  - Created at 23:58:19Z: `AdminCreateUser` with no email sent and `custom:role=PortalAdmin`. Its password was generated inside the process and never written anywhere. Its global registry row was added.
  - Deleted at 00:10:59Z with its registry row. The read-back gave `UserNotFoundException` and 0 rows.
  - Kept: one audit entry under it (`build_requested`), and the Cognito admin-activity alerts, which went to a topic with no subscribers.

### The job

- **Dispatch:** at 23:58:36Z the dispatcher sent SSM command `eaf0d27f-0a29-45b1-b64c-b304373d0ffa` (`AWS-RunShellScript`) to `i-0758e9dd583c81d90`. Its final status is `Success`, with 0 errors. The log is in `/dda/portal-builds`, stream `eaf0d27f-0a29-45b1-b64c-b304373d0ffa/i-0758e9dd583c81d90/aws-runShellScript/stdout`.
- **Source:** the log shows, in order:
  - the build lock;
  - the preflight (865 GB free);
  - `Syncing source to ref '16f3e8ab391a3d184530c8ae9fb01057b6fe6f04'...`;
  - `HEAD is now at 16f3e8ab Merge pull request #174 from awslabs/feature/car-assembly-check` and `✓ Source synced to 16f3e8ab (16f3e8ab391a3d184530c8ae9fb01057b6fe6f04)`;
  - the pre-build prune, which removed only the workspace `custom-build/` (144K);
  - `Starting portal-build.sh aarch64 5 (target JP5, …)`.
- **Status:** `queued`, then `building` (23:58:36Z), then `publishing` (seen at the 00:09:42Z poll), then `succeeded` (00:09:59Z).
- **Publish:** the artifacts were 5.8G, so the Fleet took the ECR + S3 path. The log shows, in order:
  - `Publishing version: 1.0.4` at 00:09:39Z;
  - the two image pushes;
  - the scripts zip;
  - `Creating component version via API...`, answered with `"componentVersion": "1.0.4"`;
  - `PORTAL_BUILD_RESULT {"component_name":"aws.edgeml.dda.LocalServer.arm64JP5","published_version":"1.0.4",…}`.

### After the job

- **The job detail** (`GET /builds/45e87f6a-…` at 00:10:43Z): `succeeded`, `result.published_version` `1.0.4`, `result.pushed_image_refs` the two images, no error, all terminal effects done.
- **Deviation, the resolved commit:** the job detail has no `source_commit`, so its **Resolved commit** shows `-`. The agent did emit it: its `building` event at 23:58:46Z carries `"source_commit":"16f3e8ab391a3d184530c8ae9fb01057b6fe6f04"` (`git rev-parse HEAD` after the sync).
  - **Why it is missing:** the deployed `build_events.apply_phase_event` (`16f3e8ab`, L340–L400) treats that event as a no-op for a dedicated job, which the dispatcher has already moved to `building`. It returns before it persists `source_commit`, so no dedicated job of this Portal records the commit. None of the five earlier JP5 items has it either.
  - **Where it was read instead:** the log stream, as the plan provided. The commit equals `<DEPLOY_COMMIT>`, so the published version is not a stray.
  - **Status of the defect:** it is in the Portal's build code, outside this spec. It is reported here and not fixed.
- **The JP5 items read again** (00:11:19Z, `state-19-1/precheck-after-job/summary.txt`): the five above, unchanged, plus `45e87f6a-7110-4406-afef-b74c2f26f0a8`: succeeded, requester `tmp-cac-191-build-20261127T235814Z`, ref `16f3e8ab391a3d184530c8ae9fb01057b6fe6f04`, version `1.0.4`, ended 2026-11-28 00:09:59Z. None is running.
- **The lineage:** `list-component-versions` gives `1.0.4`, `1.0.3`, `1.0.2`, `1.0.1`, `1.0.0`, so `1.0.4` is listed. It is also the highest version at this read. That is recorded but is not a check, because the lineage is shared. No higher version was seen.
- **The component:** `describe-component` of `…:versions:1.0.4` shows it created 2026-11-28 00:09:56Z, `DEPLOYABLE`, platform linux/aarch64.

### Effect and undo

- **Effect:**
  - `1.0.4` is now the highest version of the shared `arm64JP5` lineage, so **Update to latest** on any `arm64JP5` device, `mic-730ai` included, installs it. The owner was asked to tell the zone3 session.
  - No device, deployment, floor, stack or function changed.
  - On the shared build server, the checkout moved from `268dc5f5` to `16f3e8ab` (detached), and its workspace was pruned, as every Fleet build does.
- **Next:**
  - 19.3 sets both floor entries of the demo deployment to `{"arm64_jp5": "1.0.4"}`.
  - 19.4 pins `mic-730ai` to `1.0.4`, behind gate 0.15.
- **Undo** (not run; each step needs its own go):
  1. `aws greengrassv2 delete-component --arn arn:aws:greengrass:us-west-2:326334468650:components:aws.edgeml.dda.LocalServer.arm64JP5:versions:1.0.4`, which is possible only while no deployment names the version.
  2. Then the two ECR tags (`aws ecr batch-delete-image`) and the S3 object.
