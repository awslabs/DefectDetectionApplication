# Design Document: Security Scan Remediation (HIGH)

## Overview

This design remediates the HIGH-severity findings of security scan `d5f01064-c0bb-4aac-91ff-5f55b65e2afd` (run 2026-10-02, exported 2026-10-03) on the `remediation` branch, cut from `integration/all-specs` at `4a3f960`. It implements the owner-approved `requirements.md` (18 requirements, 86 acceptance criteria) and builds on the Triage_Ledger in this directory: `ledger.json` holds one entry per finding, and `ledger.md` summarizes it by rule. Findings are referred to by rule id and `finding_id` only.

Triage is complete. Each of the 441 findings in the Scan_Export has exactly one ledger entry: 30 are `REMEDIATE`, 411 are `FALSE_POSITIVE`, and none is `IGNORED_IMPAIRS_FUNCTION`. No finding met Requirement 4.1, because wherever a fix was needed and possible with public dependencies, one exists that keeps the function working (Requirement 4.3). The false positives need no code change; each entry carries the reason that closes it. The 30 `REMEDIATE` findings fall into nine remediation areas. Each area has its own section in this design, and every `REMEDIATE` entry's `design_ref` points at one of them.

| Area | Requirement | `REMEDIATE` findings | Code that changes | Where it runs |
|---|---|---|---|---|
| [R6 JWT authorizer](#r6-jwt-authorizer) | 6 | 1 | `edge-cv-portal/backend/functions/jwt_authorizer.py`; the `ALLOWED_AUDIENCES` wiring in `edge-cv-portal/infrastructure/lib/compute-stack.ts` and `edge-cv-portal/infrastructure/bin/app.ts` | Portal Lambda `JwtAuthorizerHandler`; the triage found that no deployed API uses it today |
| [R7 process execution](#r7-process-execution) | 7 | 4 | `datasets/detection_training/export_checkpoint.py` (two findings on one call) and `edge-cv-portal/detector-export-image/Dockerfile`; `src/backend/workflow_engine/python_bridge.py`; `src/backend/utils/dda_user_management_utils.py`, `src/backend/utils/user_group_management_utils.py` and `src/backend/resources/accessors/image_source_accessor.py`, the callers of the flagged `src/backend/utils/utils.py`, which doesn't change | LocalServer; the detector export image that the Portal runs as a SageMaker job |
| [R9 URL fetching](#r9-url-fetching) | 9 | 2 | `src/backend/workflow_engine/payload_fetch.py`, `test/on-hardware/register_vllm_models.py` | LocalServer workflow engine; a seed script that `README.md` tells operators to run |
| [R10 deserialization](#r10-deserialization) | 10 | 6 | Two byte-identical copies of `reference_image_map_migration.py`, under `src/backend/lyra_science_processing_utils/model_processors/` and `edge-cv-portal/test-sandbox/dda_triton_resources/lyra_science_processing_utils/model_processors/`; three findings each | Offline migration CLI for legacy reference-image maps, run by an operator; the `src/backend` copy ships in the LocalServer image, and the inference path never imports it |
| [R12 identifier hashes](#r12-identifier-hashes) | 12 | 2 | `src/backend/camera_discovery/discovery.py`, `src/backend/camera_discovery/aravis.py` and a new `src/backend/camera_discovery/stable_hash.py` | LocalServer camera discovery |
| [R14 SNS encryption](#r14-sns-encryption) | 14 | 2 | Topic `dda-portal-training-alerts` in `edge-cv-portal/infrastructure/lib/compute-stack.ts`; `CKV_AWS_26` and `scanner-x/sns-topic-encryption` flag the same resource | ComputeStack (`EdgeCVPortalComputeStack`) |
| [R14 SQS encryption](#r14-sqs-encryption) | 14 | 6 | Queues `dda-portal-camera-shadow-reports`, `dda-portal-account-sync-acks` and `dda-portal-autolabel-queue`, and their three dead-letter queues, in `compute-stack.ts` | ComputeStack |
| [R14 DynamoDB CMK](#r14-dynamodb-cmk) | 14 | 2 | Tables `dda-portal-edge-credentials` and `dda-portal-account-sync` in `compute-stack.ts` | ComputeStack |
| [R15 least-privilege IAM](#r15-least-privilege-iam) | 15 | 5 | The `StationProvisioningRole` IoT grant, the `DevicesHandler` secure-tunneling grant and the SageMaker EventBridge-enabler custom resource's grant in `compute-stack.ts`; `DDASageMakerExecutionRole` in `edge-cv-portal/infrastructure/lib/usecase-account-stack.ts` | ComputeStack; use-case account stack (`DDAPortalUseCaseAccountStack`) |
| Total | | 30 | | |

These constraints from the requirements apply to every area:

- CloudFormation changes go into the CDK source. Every Checkov and `scanner-x/sns-topic-encryption` finding is reported against a committed template fixture, but the fix goes into the construct that produces the resource. Baseline_Templates and approval files change only through the project's existing process, with owner approval where that process asks for it (Requirements 15.6 and 16.3). Unfixed_Snapshots are never hand-edited (Requirement 16.4).
- Every flagged CloudFormation resource is in ComputeStack or the use-case account stack. Changes deploy as in-place updates that keep data, messages, subscriptions and resource names (Requirement 16.2), within CloudFormation's template-size and resource-count limits (Requirement 16.1).
- Device-side changes run on every supported Device_Runtime (Requirement 16.5).
- No change points a Dockerfile, build script or deployment at a company-internal registry or service (Requirement 2.4).
- User-facing behavior changes only where a ledger entry records the change as deliberate (Requirement 16.6).
- No inline scanner suppression or scanner-configuration exclude is added until the owner approves that approach (Requirement 18.4). The branch stays off the public remote while this spec describes unremediated vulnerabilities, unless the owner decides otherwise (Requirement 18.3).

The technology stack is what the affected components already use, and no third-party dependency is added. Python changes use the standard library and libraries the modules already import, such as PyJWT in the authorizer. Portal Lambdas in ComputeStack run on the Python 3.11 and 3.12 Lambda runtimes. The device-side modules that change (`dda_user_management_utils.py`, `user_group_management_utils.py`, `image_source_accessor.py`, `python_bridge.py`, `payload_fetch.py`, `discovery.py`, `aravis.py`, the new `stable_hash.py`, and the `src/backend` copy of `reference_image_map_migration.py`) ship in the LocalServer image. That image's interpreter is CPython 3.11 on JetPack 5, JetPack 7 and x86, and 3.10 on JetPack 6. On JetPack 5 the image builds 3.11 from source (`src/backend/Dockerfile.jp5`), since Ubuntu 20.04 on arm64 has no packaged 3.11; the host's own system Python there is 3.8. Requirement 12.3 still asks that the hash marking also run on Python 3.8. CDK changes are TypeScript on the existing `aws-cdk-lib` v2 modules (`aws-kms`, `aws-sns`, `aws-sqs`, `aws-dynamodb`, `aws-iam`). Tests use the existing pytest and Hypothesis suites and the Jest infrastructure tests in `edge-cv-portal/infrastructure`.

### Scope

In scope:

- The 30 `REMEDIATE` findings in the nine areas above. The full list, with finding ids, is in [Results](#results).
- Closing the 411 `FALSE_POSITIVE` findings through their ledger entries, with no code change.
- Verification and closure under Requirement 17: affected suites compared with a baseline run at `4a3f960`, infrastructure tests after `npm run build`, the IAM preservation synth gate on the host, a local rescan with Bandit, Semgrep OSS and Checkov, and the platform rescan that the owner runs.
- Public-repository hygiene under Requirement 18.

Requirements 2, 5, 8, 11 and 13 have no `REMEDIATE` entry, so nothing changes under them:

- Requirement 2: the 15 `scanner-x/docker-image-source` findings are `open-source-constraint`. Each entry records the image reference and the public registry that serves it.
- Requirement 5: none of the 268 credential findings is a real credential (205 `test-only`, 63 `scanner-misread`), so the rotation step in Requirement 5.4 doesn't apply.
- Requirement 8: the 13 `exec` findings are in Test_Code. The 3 `eval` findings are in unmodified third-party packages (`attrs`, `typing_extensions`) vendored into the `workflow_core` Lambda layer.
- Requirement 11: of the 11 SQL findings, 8 are `test-only` and 3 are `scanner-misread`.
- Requirement 13: the 4 `B104` findings are all-zeros address strings in test fixtures, not binds. The 2 `scanner-x/plaintext-http` findings are example LAN URLs inside HTML comments in `hmi/index.html` and `hmi/triple.html`.

Out of scope:

- MEDIUM and LOW findings, which would get sibling specs with the same layout.
- Anything not in this export, including results the platform may have filtered out before exporting.
- Observations: issues the triage noticed that the scan didn't report. This design records them but doesn't fix them.

### Owner decisions before implementation

These need the owner's answer before the affected tasks start. Where the triage recorded a default, it's given. The owner answered all of them on 2026-10-04. `requirements.md` records the answers as owner decisions 1 to 8, and each bullet below ends with its answer and that number.

- Suppressions (open question 1, Requirement 18.4): inline suppressions, scanner-configuration excludes for test directories, or neither. The scanning platform may ignore inline suppressions. Default until approved: none. Decided 2026-10-04 (owner decision 1): none; false positives close through their ledger entries only.
- Customer managed KMS keys (open question 2, Requirement 4.5): Requirement 14.5 puts both flagged tables on a customer managed key, which adds a monthly key charge and request charges; one key can serve both tables ([R14 DynamoDB CMK](#r14-dynamodb-cmk)). The topic's two publishers are same-account Lambda roles, which the AWS managed SNS key allows. A customer managed key is needed there only if an operator has wired an AWS service publisher to the exported topic ARN outside the repository, which the code can't show ([R14 SNS encryption](#r14-sns-encryption)). Decided 2026-10-04 (owner decision 2): approved. One customer managed key serves both tables, with approvals-file edits A1 to A3. The topic keeps the AWS managed key unless the read-only checks find an AWS service publisher; then it takes a customer managed key and edit O1 applies.
- SQS rescan ([R14 SQS encryption](#r14-sqs-encryption)): in Checkov 3.2.255, CloudFormation `CKV_AWS_27` passes only when `KmsMasterKeyId` is set and doesn't read `SqsManagedSseEnabled`, as a local probe confirmed. For any queue that uses SSE-SQS, the owner decides how the finding left on rescan is closed. Decided 2026-10-04 (owner decision 5): the camera-shadow and account-sync-ack pairs use SSE-SQS, and the `CKV_AWS_27` results that Checkov 3.2.255 still reports on those four queues are recorded in the Rescan_Record as `scanner-misread`. The auto-label pair, whose senders are all in the portal account, takes `alias/aws/sqs`.
- IAM fixtures ([R15 least-privilege IAM](#r15-least-privilege-iam), Requirements 15.6 and 16.3): scoping the flagged statements removes wildcard grants that both Baseline_Templates record. `test_synth_iam_statements_match_fixed_baseline` rejects that, and `iam_post_fix_approved_additions.json` can't excuse it. Refreshing the baselines changes the drift that `test_baseline_drift_confined_to_I1_I4` pins to `iam_baseline_cdk_i_changes.json`, and the Unfixed_Snapshots can't be edited. The owner approves the fixture path before any fixture or approval file changes. Decided 2026-10-04 (owner decision 6): the post-fix record path is approved as the extension in Requirement 16.3, whose wording `requirements.md` now carries.
- Unused authorizer ([R6 JWT authorizer](#r6-jwt-authorizer)): the alternative to hardening is deleting the unattached `JwtAuthorizerHandler` and `jwt_authorizer.py`. That removes the documented path for custom identity providers, so it needs approval. Default: harden. Decided 2026-10-04 (owner decision 4): harden.
- Migration utility ([R10 deserialization](#r10-deserialization)): the alternative to confining the legacy-map read is dropping both copies of the utility if no legacy maps remain in use. That removes the conversion path that the postprocessor's error message sends operators to, so it needs approval. Default: confine the read. Decided 2026-10-04 (owner decision 8): confine the read, fixing both copies.
- Folder image-source roots ([R7 DDA permission walk](#r7-dda-permission-walk)): the design confines Folder locations to `/aws_dda`, outside two root-owned subtrees, which covers every location the device web UI creates. The check runs only when a source is created or updated. The triage found no inventory of locations created through the device API. If a station has one outside the area, the owner chooses between widening the area and migrating that source. Decided 2026-10-04 (owner decision 8): confine the walk to `/aws_dda` as designed; a source found outside it moves under `/aws_dda`.
- Residual risk in `payload_fetch.py` ([R9 URL fetching](#r9-url-fetching)): an empty `allowed_uri_prefixes` keeps its documented meaning of allowing every remote source, because default-deny would break nodes configured without prefixes. The owner accepts this as residual risk. Decided 2026-10-04 (owner decision 7): accepted. The `bedrock_inference` node's `allowed_uri_prefixes` description in the workflow catalog, which the Portal workflow designer shows (`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`, mirrored byte for byte in `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`), says that an empty list lets a trigger payload point the fetch at any remote source and recommends setting prefixes.
- Scanner names (open question 3): before the branch is pushed, keep the scanner names and rule ids in this public spec, including Scanner-X's `scanner-x/...` rules, or switch to neutral labels. Decided 2026-10-04 (owner decision 3): Bandit, Semgrep OSS and Checkov keep their names; Scanner-X, its `scanner-x/...` rules and `scanner-x/sns-topic-encryption` take neutral labels as the last step before any push, and the mapping stays outside the repository.

## Triage method and results

Triage counted the 441 findings in the Scan_Export and wrote exactly one ledger entry for each, organized by rule: 30 rule ids from four scanners, triaged in 23 rule groups and merged into `ledger.json`. The subsections below describe how each finding was located and how its Disposition was decided. The [Results](#results) tables are computed from `ledger.json` by a script that loads the file and counts its fields; they aren't transcribed from `ledger.md`.

### Resolving findings to tracked code

The export names file basenames and lines but no commit, so each finding was resolved against the git-tracked files of `remediation` at `4a3f960` (Requirement 1.3). Each scanner needed a different method:

- Bandit (318 findings): Bandit 1.8.6 was re-run locally over every tracked `*.py`, limited to the 12 exported rule ids. Each export finding was paired with a local result for the same basename and rule, nearest line first. For `B105`, `B106` and `B107` the flagged value also had to match. Every pairing was confirmed by reading the code at the resolved line.
- Checkov (29): each reported template basename matches exactly one tracked file under `test/backend-test/security/baselines/`. A local Checkov 3.2.255 run over copies of the four templates reproduced all 29 findings at the same lines and logical ids, so no line drifted.
- Semgrep OSS (75): not re-run, because its registry rules aren't available offline. The flagged call or literal was matched at or near the reported line in every tracked file with that basename, then confirmed by reading the code. Where a basename has several tracked candidates (12 `conftest.py` files, 3 `utils.py` files), only one has a matching call at the reported line.
- Scanner-X (19): its rule set isn't public, so it wasn't re-run. The 15 Dockerfile findings name 7 basenames that map to 11 tracked Dockerfiles. Where a basename exists in two directories, the finding resolves to the file whose reported line is a `FROM` instruction, and `resolution_note` records the other candidate. The 2 HTML findings were matched against the `http://` occurrences in tracked HTML. The 2 `scanner-x/sns-topic-encryption` findings sit on the same template lines as the `CKV_AWS_26` findings for the same topic.

All 441 findings resolved to tracked files, so no entry needed `not-found` or `untracked` (Requirement 1.4). The 10 `vendored-or-untracked` entries are all vendored packages that git tracks in the `workflow_core` layer (`attrs`, `typing_extensions`, `jsonschema`). Ledger line numbers are those at `4a3f960`, and 158 entries carry a `resolution_note` on how their location was chosen.

Several findings can share one location (Requirement 1.6). 13 file-and-line locations carry more than one finding, 28 entries in all. Each finding keeps its own entry and lists the others in `duplicates`. For example, `CKV_AWS_26` `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-7` and `scanner-x/sns-topic-encryption` `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-0` both flag the training-alerts topic. Separately, 14 entries use `related_copies` to link the matching finding in another tracked copy of the same file, such as the second `reference_image_map_migration.py`.

### Deciding each Disposition

Each Disposition was decided from the code at the resolved location, never from the rule id or file name alone (Requirement 1.5). The triage first settled whether the file is Shipped_Code or Test_Code from what packages or runs it: Dockerfile `COPY` lines, Lambda asset and layer paths, component recipes, and documentation that tells users to run a script. It then applied these tests in order:

1. Not a real vulnerability: `FALSE_POSITIVE` with the Sub_Reason that fits (Requirement 3). A `scanner-misread` entry states what the value is. A `test-only` entry states why no shipped artifact includes the file. A `vendored-or-untracked` entry names the file's origin. An `already-mitigated` entry cites the path and line of the existing control. A `platform-constraint` entry names the platform limit.
2. Real, but fixable only with company-internal infrastructure: `FALSE_POSITIVE` with `open-source-constraint` (owner rule 1, Requirement 2).
3. Real, with every remediation impairing a function users rely on: `IGNORED_IMPAIRS_FUNCTION` (owner rule 2, Requirement 4). A remediation that only adds cost becomes an owner decision instead (Requirement 4.5).
4. Everything else: `REMEDIATE`, with a `design_ref` to its area in this design (Requirement 1.11). `task_ref` stays empty until tasks exist.

Sorting by file name would not have matched these results. 43 findings in `test_*.py` or `conftest.py` files aren't `test-only`: 39 are `scanner-misread`, and 4 are in a vendored package's own tests. 22 `test-only` findings sit in files without a test name, 16 of them in Unfixed_Snapshots. The 15 Baseline_Template findings sit under `test/` and are still `REMEDIATE`, because those templates mirror what the CDK stacks deploy. `test/on-hardware/register_vllm_models.py` is Shipped_Code because `README.md` tells operators to run it, so its `B310` finding `95141da5-9489-42a9-a8d5-e066182a8f68-0` is `REMEDIATE`.

Some rules needed extra checks:

- Credential-format values (Requirement 3.6): the two AWS-key-format literals (`generic.secrets.security.detected-aws-access-key-id-value` `bd0dcbb0-69c0-48d1-99fa-32ace92e33c4-4` and `6b6f8e8b-0f57-4fa7-bd9d-de2c73f697b9-4`) are patterned canary values. The JWT-shaped string (`generic.secrets.security.detected-jwt-token` `389d8d79-a874-4aab-822c-36676b8eb465-2`) has no valid claims or signature segments, so it isn't a signed token. All were judged by inspection, and nothing was used to authenticate.
- Subprocess calls (Requirement 7.4): for the two subprocess rules, `scanner-misread` means no outside party controls any non-literal argument. Such arguments are the running interpreter, a bundled module path, a module constant, or a temp path the same code creates. `already-mitigated` means an externally influenced argument exists and a cited check prevents injection. `REMEDIATE` means an externally influenced argument reaches the call unchecked. 16 subprocess entries list each non-literal argument, its source, and whether it's externally influenced; they include every flagged call in Shipped_Code.
- Every `FALSE_POSITIVE` entry has a reason for that finding (Requirement 1.10). 407 of the 411 reason texts are distinct. The shared ones belong to findings reported more than once on the same Dockerfile line, and to two findings in the same vendored package's tests. No entry copies export description text (Requirement 1.12).

### Template findings

31 findings are reported against CloudFormation fixtures: all 29 Checkov findings and the 2 `scanner-x/sns-topic-encryption` findings. 15 are against the two Baseline_Templates (14 ComputeStack, 1 use-case account stack). The other 16 are against their Unfixed_Snapshots (15 and 1).

- Baseline_Template findings (Requirement 1.7): each flagged logical id was traced to the construct in `edge-cv-portal/infrastructure/lib/` that produces it, and the Disposition was decided from that construct. All 15 are `REMEDIATE`, fixed in the CDK source.
- Unfixed_Snapshot role: this design confirms the glossary's reading. Each Unfixed_Snapshot is the pre-fix template of its stack. It is read only by `test_baseline_drift_confined_to_I1_I4` in `test/backend-test/security/preservation/test_preservation_iam_cdk_synth.py`, which compares IAM statements between the unfixed and fixed baselines. It is never deployed. All 16 Unfixed_Snapshot findings are `FALSE_POSITIVE` with `test-only`.
- Cross-references (Requirements 1.8 and 1.9): 13 of the 16 point at the Baseline_Template finding for the same resource. The other 3 have no Baseline_Template counterpart: `CKV_AWS_107` `950b9d60-0315-4329-9633-02b8aa44298c-1`, `CKV_AWS_108` `950b9d60-0315-4329-9633-02b8aa44298c-2` and `CKV_AWS_111` `950b9d60-0315-4329-9633-02b8aa44298c-6`. They flag `DeviceRegistrationsRoleDefaultPolicyFFA79FC4`, which the earlier IAM authorization fix rewrote (changes I1 and I2). The same logical id passes all four IAM checks in the Baseline_Template, so Requirement 1.9 adds no remediation beyond the 15 Baseline_Template findings.

### Ledger checks

Each of the 23 rule groups was triaged separately. A merge script rebuilds `ledger.json` from the group results on every run and stops if it finds any of these:

- A group whose entry count differs from its expected count.
- A finding id that is missing from the groups, absent from the export, or present in two groups.
- A rule that doesn't match the export record.
- A Disposition or Sub_Reason outside the allowed values.
- A `FALSE_POSITIVE` without a reason, or an `IGNORED_IMPAIRS_FUNCTION` entry without the impaired function, affected users and mechanism.
- A `REMEDIATE` entry without `design_ref`, or any `task_ref` set before tasks exist.
- A `scanner-x/docker-image-source` entry not closed under owner rule 1.
- A Shipped_Code subprocess entry without its external inputs.
- Copied export text or internal-looking references.

A separate read-only checker, kept with the Scan_Export outside the repository, re-checks the finished deliverables. It confirms exact coverage of the 441 ids, the allowed values, and the required `FALSE_POSITIVE` and `IGNORED_IMPAIRS_FUNCTION` fields. It also confirms that every `design_ref` anchor matches a heading in this design, that `ledger.md` counts equal `ledger.json` counts, and that `requirements.md` is unchanged. Finally, it runs the internal-reference grep over the spec directory (Requirement 18.1) and confirms that no export sentence was copied (Requirement 1.12).

### Results

The Dispositions and Sub_Reasons total as follows:

| Disposition | Entries |
|---|---|
| `REMEDIATE` | 30 |
| `FALSE_POSITIVE` | 411 |
| `IGNORED_IMPAIRS_FUNCTION` | 0 |
| Total | 441 |

| `FALSE_POSITIVE` Sub_Reason | Entries |
|---|---|
| `test-only` | 302 |
| `scanner-misread` | 79 |
| `open-source-constraint` | 15 |
| `vendored-or-untracked` | 10 |
| `already-mitigated` | 5 |
| `platform-constraint` | 0 |
| Total | 411 |

`platform-constraint` has no entries. Of the 13 IAM findings, the five against Baseline_Templates each flag actions that can be scoped, and the eight against Unfixed_Snapshots are `test-only`, so none qualifies under Requirement 15.3. Because no entry is `IGNORED_IMPAIRS_FUNCTION`, no entry needs the fields that Requirement 4.2 asks for.

By scanner:

| Scanner | Findings | `REMEDIATE` | `FALSE_POSITIVE` | `IGNORED_IMPAIRS_FUNCTION` |
|---|---|---|---|---|
| Bandit | 318 | 8 | 310 | 0 |
| Semgrep OSS | 75 | 7 | 68 | 0 |
| Checkov | 29 | 14 | 15 | 0 |
| Scanner-X | 19 | 1 | 18 | 0 |
| Total | 441 | 30 | 411 | 0 |

By requirement area, using the rule-to-requirement mapping of the finding inventory in `requirements.md`. The `platform-constraint` and `IGNORED_IMPAIRS_FUNCTION` columns are left out because they're zero everywhere.

| Requirement | Findings | `REMEDIATE` | test-only | scanner-misread | open-source-constraint | vendored-or-untracked | already-mitigated |
|---|---|---|---|---|---|---|---|
| 2 | 15 | 0 | 0 | 0 | 15 | 0 | 0 |
| 5 | 268 | 0 | 205 | 63 | 0 | 0 | 0 |
| 6 | 1 | 1 | 0 | 0 | 0 | 0 | 0 |
| 7 | 64 | 4 | 48 | 6 | 0 | 5 | 1 |
| 8 | 16 | 0 | 13 | 0 | 0 | 3 | 0 |
| 9 | 10 | 2 | 2 | 0 | 0 | 2 | 4 |
| 10 | 14 | 6 | 7 | 1 | 0 | 0 | 0 |
| 11 | 11 | 0 | 8 | 3 | 0 | 0 | 0 |
| 12 | 5 | 2 | 3 | 0 | 0 | 0 | 0 |
| 13 | 6 | 0 | 0 | 6 | 0 | 0 | 0 |
| 14 | 18 | 10 | 8 | 0 | 0 | 0 | 0 |
| 15 | 13 | 5 | 8 | 0 | 0 | 0 | 0 |
| Total | 441 | 30 | 302 | 79 | 15 | 10 | 5 |

By rule. The per-rule totals match the finding inventory in `requirements.md`.

| Rule | Scanner | Requirement | Findings | `REMEDIATE` | test-only | scanner-misread | open-source-constraint | vendored-or-untracked | already-mitigated |
|---|---|---|---|---|---|---|---|---|---|
| `scanner-x/docker-image-source` | Scanner-X | 2 | 15 | 0 | 0 | 0 | 15 | 0 | 0 |
| `B105` | Bandit | 5 | 252 | 0 | 196 | 56 | 0 | 0 | 0 |
| `B106` | Bandit | 5 | 11 | 0 | 5 | 6 | 0 | 0 | 0 |
| `generic.secrets.security.detected-aws-access-key-id-value` | Semgrep OSS | 5 | 2 | 0 | 2 | 0 | 0 | 0 | 0 |
| `B107` | Bandit | 5 | 1 | 0 | 0 | 1 | 0 | 0 | 0 |
| `generic.secrets.security.detected-jwt-token` | Semgrep OSS | 5 | 1 | 0 | 1 | 0 | 0 | 0 | 0 |
| `python.jwt.security.jwt-python-hardcoded-secret` | Semgrep OSS | 5 | 1 | 0 | 1 | 0 | 0 | 0 | 0 |
| `python.jwt.security.unverified-jwt-decode` | Semgrep OSS | 6 | 1 | 1 | 0 | 0 | 0 | 0 | 0 |
| `python.lang.security.audit.dangerous-subprocess-use-audit` | Semgrep OSS | 7 | 62 | 3 | 48 | 5 | 0 | 5 | 1 |
| `B604` | Bandit | 7 | 1 | 0 | 0 | 1 | 0 | 0 | 0 |
| `python.lang.security.audit.dangerous-subprocess-use-tainted-env-args` | Semgrep OSS | 7 | 1 | 1 | 0 | 0 | 0 | 0 | 0 |
| `B102` | Bandit | 8 | 13 | 0 | 13 | 0 | 0 | 0 | 0 |
| `B307` | Bandit | 8 | 3 | 0 | 0 | 0 | 0 | 3 | 0 |
| `B310` | Bandit | 9 | 10 | 2 | 2 | 0 | 0 | 2 | 4 |
| `B403` | Bandit | 10 | 7 | 2 | 4 | 1 | 0 | 0 | 0 |
| `B301` | Bandit | 10 | 5 | 2 | 3 | 0 | 0 | 0 | 0 |
| `python.lang.security.deserialization.avoid-dill` | Semgrep OSS | 10 | 2 | 2 | 0 | 0 | 0 | 0 | 0 |
| `B608` | Bandit | 11 | 6 | 0 | 5 | 1 | 0 | 0 | 0 |
| `python.sqlalchemy.security.sqlalchemy-execute-raw-query` | Semgrep OSS | 11 | 5 | 0 | 3 | 2 | 0 | 0 | 0 |
| `B324` | Bandit | 12 | 5 | 2 | 3 | 0 | 0 | 0 | 0 |
| `B104` | Bandit | 13 | 4 | 0 | 0 | 4 | 0 | 0 | 0 |
| `scanner-x/plaintext-http` | Scanner-X | 13 | 2 | 0 | 0 | 2 | 0 | 0 | 0 |
| `CKV_AWS_27` | Checkov | 14 | 10 | 6 | 4 | 0 | 0 | 0 | 0 |
| `CKV_AWS_119` | Checkov | 14 | 4 | 2 | 2 | 0 | 0 | 0 | 0 |
| `CKV_AWS_26` | Checkov | 14 | 2 | 1 | 1 | 0 | 0 | 0 | 0 |
| `scanner-x/sns-topic-encryption` | Scanner-X | 14 | 2 | 1 | 1 | 0 | 0 | 0 | 0 |
| `CKV_AWS_111` | Checkov | 15 | 9 | 4 | 5 | 0 | 0 | 0 | 0 |
| `CKV_AWS_109` | Checkov | 15 | 2 | 1 | 1 | 0 | 0 | 0 | 0 |
| `CKV_AWS_107` | Checkov | 15 | 1 | 0 | 1 | 0 | 0 | 0 | 0 |
| `CKV_AWS_108` | Checkov | 15 | 1 | 0 | 1 | 0 | 0 | 0 | 0 |
| Total | | | 441 | 30 | 302 | 79 | 15 | 10 | 5 |

The 30 `REMEDIATE` entries, by area. Template findings are listed by template basename and line, with the CDK source lines at `4a3f960` that produce the resource. CDK paths are relative to `edge-cv-portal/infrastructure/lib/`.

| Area | Rule | finding_id | Resolved to | Changed in |
|---|---|---|---|---|
| [R6](#r6-jwt-authorizer) | `python.jwt.security.unverified-jwt-decode` | `490a14e8-75e3-48e4-af23-ae95676b238c-0` | `edge-cv-portal/backend/functions/jwt_authorizer.py:153` | same file; CDK wiring in `compute-stack.ts:38` and `:1268` and `edge-cv-portal/infrastructure/bin/app.ts` |
| [R7](#r7-process-execution) | `python.lang.security.audit.dangerous-subprocess-use-audit` | `feb1c815-fb14-44af-b148-6502a6f1fad8-0` | `datasets/detection_training/export_checkpoint.py:445` | same file; `edge-cv-portal/detector-export-image/Dockerfile:37-38` |
| [R7](#r7-process-execution) | `python.lang.security.audit.dangerous-subprocess-use-tainted-env-args` | `feb1c815-fb14-44af-b148-6502a6f1fad8-1` | `datasets/detection_training/export_checkpoint.py:446` | same file; `edge-cv-portal/detector-export-image/Dockerfile:37-38` |
| [R7](#r7-process-execution) | `python.lang.security.audit.dangerous-subprocess-use-audit` | `24e13eb5-0be6-4586-abc1-9ca6aa3b9aab-0` | `src/backend/utils/utils.py:157` | The callers: `src/backend/utils/dda_user_management_utils.py`, `src/backend/utils/user_group_management_utils.py` and `src/backend/resources/accessors/image_source_accessor.py`. `utils.py` doesn't change |
| [R7](#r7-process-execution) | `python.lang.security.audit.dangerous-subprocess-use-audit` | `69ef51f8-d65b-4291-a4ae-d447bbd2367d-0` | `src/backend/workflow_engine/python_bridge.py:1043` | same file |
| [R9](#r9-url-fetching) | `B310` | `f035ce5a-9b67-4aac-9ec0-b5f597f0fdcc-0` | `src/backend/workflow_engine/payload_fetch.py:301` | same file |
| [R9](#r9-url-fetching) | `B310` | `95141da5-9489-42a9-a8d5-e066182a8f68-0` | `test/on-hardware/register_vllm_models.py:128` | same file |
| [R10](#r10-deserialization) | `B403` | `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-1` | `edge-cv-portal/test-sandbox/dda_triton_resources/lyra_science_processing_utils/model_processors/reference_image_map_migration.py:60` | same file |
| [R10](#r10-deserialization) | `B301` | `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-0` | `edge-cv-portal/test-sandbox/dda_triton_resources/lyra_science_processing_utils/model_processors/reference_image_map_migration.py:68` | same file |
| [R10](#r10-deserialization) | `python.lang.security.deserialization.avoid-dill` | `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-2` | `edge-cv-portal/test-sandbox/dda_triton_resources/lyra_science_processing_utils/model_processors/reference_image_map_migration.py:68` | same file |
| [R10](#r10-deserialization) | `B403` | `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-4` | `src/backend/lyra_science_processing_utils/model_processors/reference_image_map_migration.py:60` | same file |
| [R10](#r10-deserialization) | `B301` | `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-3` | `src/backend/lyra_science_processing_utils/model_processors/reference_image_map_migration.py:68` | same file |
| [R10](#r10-deserialization) | `python.lang.security.deserialization.avoid-dill` | `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-5` | `src/backend/lyra_science_processing_utils/model_processors/reference_image_map_migration.py:68` | same file |
| [R12](#r12-identifier-hashes) | `B324` | `e1583a8e-82f5-468a-829e-9cc91e0e5bb7-0` | `src/backend/camera_discovery/aravis.py:105` | same file, through the new `src/backend/camera_discovery/stable_hash.py` |
| [R12](#r12-identifier-hashes) | `B324` | `93e57454-5aca-4e33-a9a0-fe6de96905c1-0` | `src/backend/camera_discovery/discovery.py:170` | same file, through the new `src/backend/camera_discovery/stable_hash.py` |
| [R14 SNS](#r14-sns-encryption) | `CKV_AWS_26` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-7` | `iam_baseline_EdgeCVPortalComputeStack.template.json:43560` | `compute-stack.ts:3448-3451` |
| [R14 SNS](#r14-sns-encryption) | `scanner-x/sns-topic-encryption` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-0` | `iam_baseline_EdgeCVPortalComputeStack.template.json:43560` | `compute-stack.ts:3448-3451` |
| [R14 SQS](#r14-sqs-encryption) | `CKV_AWS_27` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-13` | `iam_baseline_EdgeCVPortalComputeStack.template.json:29132` | `compute-stack.ts:1672` |
| [R14 SQS](#r14-sqs-encryption) | `CKV_AWS_27` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-8` | `iam_baseline_EdgeCVPortalComputeStack.template.json:29180` | `compute-stack.ts:1681` |
| [R14 SQS](#r14-sqs-encryption) | `CKV_AWS_27` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-9` | `iam_baseline_EdgeCVPortalComputeStack.template.json:31597` | `compute-stack.ts:2047` |
| [R14 SQS](#r14-sqs-encryption) | `CKV_AWS_27` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-10` | `iam_baseline_EdgeCVPortalComputeStack.template.json:31645` | `compute-stack.ts:2056` |
| [R14 SQS](#r14-sqs-encryption) | `CKV_AWS_27` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-11` | `iam_baseline_EdgeCVPortalComputeStack.template.json:34238` | `compute-stack.ts:2558` |
| [R14 SQS](#r14-sqs-encryption) | `CKV_AWS_27` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-12` | `iam_baseline_EdgeCVPortalComputeStack.template.json:34286` | `compute-stack.ts:2564` |
| [R14 DynamoDB](#r14-dynamodb-cmk) | `CKV_AWS_119` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-5` | `iam_baseline_EdgeCVPortalComputeStack.template.json:31543` | `compute-stack.ts:2015-2026` |
| [R14 DynamoDB](#r14-dynamodb-cmk) | `CKV_AWS_119` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-6` | `iam_baseline_EdgeCVPortalComputeStack.template.json:31570` | `compute-stack.ts:2031-2042` |
| [R15](#r15-least-privilege-iam) | `CKV_AWS_111` | `ec660377-3ea5-4738-962a-1e6d384643fa-0` | `iam_baseline_DDAPortalUseCaseAccountStack.template.json:110` | `usecase-account-stack.ts:250-262`, `:265-297` |
| [R15](#r15-least-privilege-iam) | `CKV_AWS_109` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-1` | `iam_baseline_EdgeCVPortalComputeStack.template.json:1717` | `compute-stack.ts:986-1002` |
| [R15](#r15-least-privilege-iam) | `CKV_AWS_111` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-2` | `iam_baseline_EdgeCVPortalComputeStack.template.json:1717` | `compute-stack.ts:986-1002` |
| [R15](#r15-least-privilege-iam) | `CKV_AWS_111` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-3` | `iam_baseline_EdgeCVPortalComputeStack.template.json:3903` | `compute-stack.ts:1104-1121` |
| [R15](#r15-least-privilege-iam) | `CKV_AWS_111` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-4` | `iam_baseline_EdgeCVPortalComputeStack.template.json:46006` | `compute-stack.ts:3636-3645` |

## r6-jwt-authorizer

This section remediates `python.jwt.security.unverified-jwt-decode` `490a14e8-75e3-48e4-af23-ae95676b238c-0` (Semgrep OSS), reported at `edge-cv-portal/backend/functions/jwt_authorizer.py:153` (Requirement 6). The default plan hardens the authorizer. Only the token header is read before verification, and every Allow decision rests on signature, expiry, issuer, audience and token-type checks against configuration. Deleting the unused authorizer is an owner option, covered at the end of the section.

### R6 current behavior

`JwtAuthorizerHandler` (`edge-cv-portal/infrastructure/lib/compute-stack.ts:1259-1272`) runs `jwt_authorizer.handler` on the Python 3.11 Lambda runtime. It uses `JwtLayer` (`compute-stack.ts:166-170`), which pins PyJWT 2.15.0, cryptography 50.0.1 and requests 2.34.2 (`edge-cv-portal/backend/layers/jwt/requirements.txt`).

Nothing invokes it:

- Every Portal REST API in `edge-cv-portal/infrastructure/lib/` uses API Gateway's `CognitoUserPoolsAuthorizer`.
- The infrastructure defines no `TokenAuthorizer`, `RequestAuthorizer` or `CfnAuthorizer`, and no method sets `authorizationScopes`.
- The `portal-jwt-role-privilege-escalation` spec kept it unattached on purpose (its design.md, Decision 6).

The function's code asset is the whole `edge-cv-portal/backend/functions` directory, and so are those of 55 `lambda.Code.fromAsset(...)` call sites in `edge-cv-portal/infrastructure/lib/` (42 of them in `compute-stack.ts`). So `jwt_authorizer.py` ships inside every one of those bundles. Only `JwtAuthorizerHandler` names it as a handler, and only that function has `JwtLayer`. Elsewhere the module is never imported, and its `import jwt` would fail there. Two consequences follow:

- Any edit to the file changes the shared asset hash. The next deploy updates the code of every function built from that asset, without changing their behavior.
- Deleting the file removes it from all of those bundles.

At `4a3f960`, `validate_jwt_token` works like this:

1. Lines 21-22 split `ALLOWED_AUDIENCES` and `ISSUER_WHITELIST` on commas without trimming, so an empty value becomes `['']`.
2. Line 152 reads the header for `kid`. Line 153, the flagged call, decodes the payload with `verify_signature: False` to read `iss`. Its inline `# nosem` comment did not suppress the platform finding.
3. Lines 164-169 choose the signing keys:
   - The configured pool's JWKS, when the unverified `iss` contains `cognito-idp.<region>.amazonaws.com/<pool id>` as a substring.
   - Otherwise `<iss>/.well-known/jwks.json`, when `iss` equals an `ISSUER_WHITELIST` entry.
4. Lines 185-195 verify the token, with these gaps:
   - Only RS256 is accepted, but the expected issuer is the token's own unverified `iss` (line 190). The issuer check compares the token with itself.
   - `verify_exp` is on, but `exp` isn't required, so a token without `exp` passes.
   - The audience check is off whenever `ALLOWED_AUDIENCES` is empty (lines 189 and 193). The deployed function sets it to `''` (`compute-stack.ts:1268`).

`handler` (line 282 onward) builds the user id, email, username, role, groups and the Allow policy only from `validate_jwt_token`'s return value, which is the signature-verified decode. So no allow decision uses unverified claims today (Requirement 6.1 holds). The signature check also still ties tokens to the configured key sets.

The gaps are against these criteria:

- Requirement 6.2: the unverified `iss` feeds the expected issuer.
- Requirement 6.3: no issuer is pinned from configuration, there is no audience or client check, and `exp` is optional.
- Requirement 6.5: the existing tests cover only a valid and a tampered token (`test/backend-test/security/preservation/test_preservation_secrets_jwt.py:417-468`).

If someone attached the authorizer with the deployed configuration, it would allow any token the user pool signs, for any app client and either token type. The pool has one app client, `dda-portal-client` (`edge-cv-portal/infrastructure/lib/auth-stack.ts:89`). The frontend sends that client's ID token (`edge-cv-portal/frontend/src/contexts/AuthContext.tsx:90-92`, `edge-cv-portal/frontend/src/services/api.ts:1325-1329`).

### R6 change

The stack stays as it is: Python 3.11, PyJWT 2.15.0 from `JwtLayer`, and CDK v2 in TypeScript. No deployed artifact gains a dependency.

Configuration is read once at import and replaces lines 19-22:

- `_csv_env(name)` returns an environment variable's comma-separated entries, trimmed, with empty entries dropped.
- `ALLOWED_AUDIENCES` holds the app client ids the Portal allows.
- `TRUSTED_ISSUERS` is an ordered tuple of `(issuer, jwks_url)` pairs:
  - The first is the configured pool, `https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}`, with `get_cognito_jwks_url(...)` as its JWKS URL. It is present when `COGNITO_USER_POOL_ID` is set.
  - Then come the `ISSUER_WHITELIST` entries that start with `https://`, each with `f"{issuer}/.well-known/jwks.json"` as today.
  - An entry that doesn't start with `https://` is dropped and logged at ERROR, because its signing keys would be fetched without TLS. Requirement 9.1 applies to that fetch too.
- If `ALLOWED_AUDIENCES` or `TRUSTED_ISSUERS` is empty, the module logs one ERROR at import saying every request will be denied.

`validate_jwt_token(token)` becomes:

1. If `ALLOWED_AUDIENCES` or `TRUSTED_ISSUERS` is empty, raise `AuthorizationError`.
2. Read the header with `jwt.get_unverified_header(token)`, and require `kid` to be a non-empty string. The header is the only part read before verification, and it is used only to select a key (Requirement 6.2). The unverified payload decode on line 153 is deleted, so no code derives `iss` from an unverified token.
3. Walk `TRUSTED_ISSUERS` in order. For each issuer, fetch its JWKS through `get_jwks_keys`, cached as today, and look up the `kid`. If a fetch fails, skip that issuer. When the key is found, verify the token against that issuer's configuration:

```python
claims = jwt.decode(
    token,
    construct_rsa_key(jwks_key),
    algorithms=["RS256"],
    audience=list(ALLOWED_AUDIENCES),
    issuer=issuer,  # the configured issuer that owns this key, never the token's iss
    options={"require": ["exp", "iss", "aud", "sub"]},
)
```

   PyJWT turns on every `verify_*` check by default when a key is given.

   - On `jwt.InvalidSignatureError`, the walk continues to the next issuer that holds the same `kid`. This covers two issuers whose key ids collide.
   - Any other `jwt.InvalidTokenError` ends the walk with a denial. Once the signature verifies under an issuer's key, the token belongs to that issuer and is judged by that issuer's configuration.
4. If the walk ends without a verified token, the request is denied with one of two errors:
   - No trusted JWKS holds the `kid`: raise `AuthorizationError("Key not found in any trusted JWKS: <kid>")`.
   - At least one did, and every verification failed with `jwt.InvalidSignatureError`: re-raise the last of them. The existing mapping turns it into `AuthorizationError("Invalid token signature")`.
5. For the Cognito issuer, require `claims.get("token_use") == "id"`, and otherwise raise `AuthorizationError("Token is not an ID token")`. ID tokens carry the client id in `aud`, which step 3 checks. Access tokens carry `client_id` and no `aud`, so step 3 already rejects them. They also lack the `email`, `cognito:username`, `custom:role` and `custom:groups` claims that `handler` reads, so allowing them would produce an identity made of placeholder values. This check makes the rule explicit instead of incidental. Tokens from `ISSUER_WHITELIST` issuers have no `token_use` convention, so they are checked by `aud` alone.
6. Return the claims.

The exception mapping on lines 201-213 stays, and `jwt.MissingRequiredClaimError` maps to `Token missing required claim: <name>`. Steps 1 to 6 run inside the existing `try`. The mapping's last clause, `except Exception` (lines 211-213), therefore also catches the `AuthorizationError`s that steps 1, 2, 4 and 5 raise themselves. It logs each one at ERROR as `Unexpected error validating token: <reason>` and re-raises it as `AuthorizationError("Token validation failed: <reason>")`, which `handler` logs at WARNING. A missing `kid` or an untrusted issuer takes that path today, and this design keeps it. Adding `except AuthorizationError: raise` ahead of the catch-all would change the module's log levels and reason text, so it isn't part of this fix. `handler`, `extract_token_from_event`, `generate_policy` and `_safe_event_metadata` don't change.

The CDK wiring changes in three places:

- `ComputeStackProps` (`compute-stack.ts:38`) gains `userPoolClientId?: string`, documented as the app client id the custom authorizer accepts.
- `compute-stack.ts:1268` becomes `ALLOWED_AUDIENCES: props.userPoolClientId ?? ''`. `ISSUER_WHITELIST` stays `''`.
- `edge-cv-portal/infrastructure/bin/app.ts` passes `userPoolClientId: authStack.userPoolClient.userPoolClientId` next to `userPool` (line 155). FrontendStack takes the same reference at line 258.

Consequences of the wiring:

- The prop is optional, so the 17 infrastructure test files that construct `ComputeStack` keep compiling. Without it, the authorizer denies every request, so a missing value fails closed.
- The reference adds a cross-stack dependency on AuthStack, which ComputeStack already has through `userPool`. While the reference exists, the client can't be replaced until the reference is removed. FrontendStack's reference already imposes the same constraint.
- No IAM statement changes, so the IAM preservation synth gate is unaffected. The template grows by one environment value (Requirement 16.1).

`edge-cv-portal/infrastructure/JWT_AUTHORIZER_SETUP.md` is updated where behavior changes: the configuration block (line 28), the validation steps (line 36) and the security notes (line 152). The updated text says that:

- `ALLOWED_AUDIENCES` is required, and CDK wires it from the user-pool client id.
- `ISSUER_WHITELIST` entries are exact `https://` issuer strings.
- Cognito tokens must be ID tokens.
- Only the header is read before verification.

`validate_jwt_token` owns the invariant that no claim is used before the token passes every check, because it is the only code that sees the raw token. `handler` only consumes its return value. The CDK stack owns supplying the allowed client id.

### R6 errors and validation

Every failure applies to one request and is recoverable by presenting a valid token. The function itself never fails. The caller, API Gateway, always receives a policy. No log line contains the token, because `_safe_event_metadata` stays the only event logging.

| Condition | Policy returned | Log |
|---|---|---|
| No token, or a malformed token or header | Deny, `principalId` `unauthorized` | WARNING `Authorization failed: <reason>` |
| A missing or non-string `kid` | Deny, `principalId` `unauthorized` | ERROR `Unexpected error validating token: <reason>` from the kept catch-all, then WARNING `Authorization failed: Token validation failed: <reason>` |
| `ALLOWED_AUDIENCES` or `TRUSTED_ISSUERS` empty | Deny | ERROR once at import. Per request, ERROR from the catch-all, then WARNING |
| JWKS fetch fails for one issuer | The issuer is skipped; Deny if no other issuer holds the `kid` | ERROR from `get_jwks_keys`, as today |
| `kid` in no trusted JWKS | Deny | ERROR from the catch-all, then WARNING |
| Bad signature, or an algorithm other than RS256 (including `none` and HS256) | Deny | WARNING |
| `exp` missing or past; `iss` not the configured issuer; `aud` missing or not allowed; `sub` missing | Deny | WARNING |
| Cognito `token_use` other than `id` | Deny | ERROR from the catch-all, then WARNING |
| Unexpected exception inside `validate_jwt_token` | Deny, `principalId` `unauthorized` | ERROR from the catch-all, without a traceback, then WARNING |
| Unexpected exception elsewhere in `handler` | Deny, `principalId` `error` (`handler`'s own catch-all) | ERROR with traceback |

External inputs:

- Bearer token (required): a string in JWT compact form, parsed only by PyJWT. API Gateway bounds its size, and the authorizer adds no limit of its own.
- `ALLOWED_AUDIENCES` (required in practice): comma-separated client ids, trimmed, with empties dropped. Empty means deny all.
- `ISSUER_WHITELIST` (optional): comma-separated issuers, trimmed. An entry that doesn't start with `https://` is ignored with an ERROR log. Matching is exact string equality, done by PyJWT's issuer check.
- `COGNITO_USER_POOL_ID` and `COGNITO_REGION` (set by CDK): without a pool id, there is no Cognito issuer.

### R6 preserving function

No client invokes the authorizer, so no client can break. If someone attaches it, Cognito ID tokens issued for `dda-portal-client` pass every check. That includes tokens for federated SSO users, which the same pool issues for the same client. The Allow policy and its context don't change. The S1 preservation tests in `test_preservation_secrets_jwt.py` pin them exactly by mocking `validate_jwt_token`.

These are the deliberate changes (Requirement 16.6). The ledger entry doesn't record them yet; it gains them through the `deliberate_changes` follow-up ([Ledger follow-ups](#ledger-follow-ups)):

- An empty `ALLOWED_AUDIENCES` now denies every request.
- Issuers must be exact `https://` strings.
- Cognito access tokens are denied.
- Tokens without `exp`, `aud` or `sub` are denied.

An operator who adds a custom identity provider must also add its client ids to `ALLOWED_AUDIENCES`.

### R6 tests

New: `edge-cv-portal/backend/tests/test_jwt_authorizer_verification.py`. These are unit tests through `handler()` that check the returned policy.

Setup:

- An RSA key pair is generated at test time with `cryptography`, tokens are signed with real RS256, and `get_jwks_keys` is monkeypatched to serve the public JWK. There is no network access.
- No key or token literal is committed. Test values are visibly fake (Requirement 5.3), and string literals avoid the variable names the credential rules match, such as `token`, `secret` and `password`. This leaves the secret rules nothing new to report.
- The module is reloaded with each test's environment, as `_load_jwt_authorizer` does in the preservation suite.

Cases:

- Accepted: a valid Cognito ID token, with a policy context equal to the one built from its claims. Also a valid token from a whitelisted `https://` issuer with an allowed `aud`.
- Tampered, denied (Requirement 6.5): a flipped signature byte, and a payload whose `custom:role` was changed after signing.
- Expiry, denied: an expired token, and a token with no `exp`.
- Issuer, denied:
  - An `iss` naming another pool.
  - An `https://` issuer that contains the configured pool path as a substring, which the old check matched.
  - A whitelisted `iss` on a token whose `kid` exists only in the pool's JWKS.
- Audience, denied: a wrong `aud`, and none.
- Key-id collision: two trusted issuers serve the same `kid`. A token signed with the second issuer's key is accepted. A token whose signature fails under both keys is denied, and the WARNING log names `Invalid token signature`.
- Other, denied:
  - A Cognito access token whose `client_id` is allowed.
  - An unknown `kid`, with `Key not found in any trusted JWKS` in the WARNING log.
  - `alg: none`.
  - HS256.
  - An empty `ALLOWED_AUDIENCES`.
  - An `http://` `ISSUER_WHITELIST` entry, which must be ignored.
- Static: an AST check that the module has no `jwt.decode` call with `verify_signature` set to `False`.
- Property (Hypothesis): any change to a valid token's payload claims, re-encoded under the original header and signature, is denied.

PyJWT isn't in the Portal backend test environment, so `PyJWT==2.15.0`, the layer's pin, is added to `edge-cv-portal/backend/requirements-dev.txt`. `cryptography` already arrives as a dependency of moto 5.1.22. The module calls `pytest.importorskip("jwt", minversion="2.8")`, and the verification run (`-rs`) must report zero skips for it, because some hosts carry PyJWT 1.x in system site-packages.

Updated tests. The first three belong to `security-secrets-credentials-jwt-fixes`; each change is a recorded repoint that keeps proving its property without weakening it.

- `test_preservation_secrets_jwt.py`, S5 block (lines 331-468):
  - `_load_jwt_authorizer_s5` sets `ALLOWED_AUDIENCES` to a test client id, and the signed claims add a matching `aud`.
  - The HS256 adapter's stage-1 branch is removed, because there is no unverified decode left. The adapter itself stays, because that runner lacks the cffi backend.
  - The comment that says the S5 fix only adds a `# nosem` comment is rewritten.
- `test/backend-test/security/test_secrets_bug_condition_exploration.py::test_unverified_decode_line_has_documented_marker` (lines 462-485) passes when no `verify_signature` `False` line exists. It still fails on an undocumented one.
- `test/backend-test/security/preservation/test_preservation_iam_out_of_scope_guard.py` pins the sha256 of `jwt_authorizer.py` (`test/backend-test/security/baselines/iam_out_of_scope_baseline.json:9`). It fails as soon as the file changes. Because that is a baseline file, the hash is re-recorded after the change through the owner (Requirement 16.3). The `utils.py` hash on line 16 stays, because [R7](#r7-process-execution) doesn't change that file.
- New Jest test `edge-cv-portal/infrastructure/test/jwt-authorizer-audience.test.ts`: with `userPoolClientId: 'test-client-id'`, the synthesized `JwtAuthorizerHandler` has `ALLOWED_AUDIENCES` set to `test-client-id`. Without the prop, it is `''`.

To verify, run:

1. The tests above, the unchanged S1 tests, and the secrets audit gate.
2. `npm run build` followed by the Jest suite in `edge-cv-portal/infrastructure` (Requirement 17.2).
3. The IAM preservation synth gate on the host (Requirement 17.3), which should report no change.

The flagged call is gone, so on rescan `unverified-jwt-decode` is expected to stop reporting.

### R6 owner option: delete the authorizer

Instead of hardening, the owner can delete the authorizer as dead code. That involves:

- In `compute-stack.ts`, removing `JwtAuthorizerHandler`, its `createLambdaRole('JwtAuthorizer')` role and `JwtLayer`. `edge-cv-portal/backend/layers/jwt/` stays, because `SyntheticJwtLayer` builds from it (`synthetic-data-stack.ts:182-186`, attached at line 425).
- Deleting `jwt_authorizer.py`, which removes it from all 55 bundles, and `JWT_AUTHORIZER_SETUP.md`.
- Correcting the stale claim in `edge-cv-portal/backend/functions/user_admin.py:7` that the admin routes sit behind the authorizer.
- Retiring the S1 and S5 tests above and the file's entries in `secrets_audit.py`.
- Updating `iam_out_of_scope_baseline.json`, whose guard fails when a recorded file disappears.
- Refreshing the ComputeStack Baseline_Template. The role's statements disappear from the synthesized template, and `test_synth_iam_statements_match_fixed_baseline` fails on any removal. So this lands on the same owner-approved fixture path that [R15](#r15-least-privilege-iam) needs (Requirements 15.6 and 16.3).

On deploy, CloudFormation deletes the function, role, policies and layer version in place, and no data is involved (Requirement 16.2). Requirement 6.5's tests become moot.

Recommendation: harden in this spec.

- Requirement 6's criteria are written for a working authorizer, including the tests showing that valid tokens are still accepted.
- Hardening touches no IAM fixture, while deletion joins the blocked R15 fixture path.
- Hardening keeps the documented path for custom identity providers.

If the owner doesn't intend to offer that path, deletion is the better end state: less code, and no idle role. The role comes from the shared `createLambdaRole` (`compute-stack.ts:338` onward), so it holds read-write grants on the Portal tables that the authorizer never uses. In that case, delete it together with the R15 baseline refresh, because both change the same fixtures.

### R6 observations

This spec doesn't fix these:

- `user_admin.py:7` says the admin routes sit behind `jwt_authorizer`, but `user-admin-api-stack.ts:57` attaches a `CognitoUserPoolsAuthorizer`.
- `get_jwks_keys` is cached with `lru_cache` for the life of the container (lines 50-51). `JWKS_CACHE_TTL = 3600` (line 25) and `JWT_AUTHORIZER_SETUP.md:59` both describe a one-hour TTL. After a key rotation, new tokens are denied until the container recycles. This fails closed.
- The authorizer's role carries the shared role's grants, described above, which the authorizer doesn't need.

## r7-process-execution

This section remediates four findings on three calls (Requirement 7):

| Rule | finding_id | Location |
|---|---|---|
| `python.lang.security.audit.dangerous-subprocess-use-audit` | `feb1c815-fb14-44af-b148-6502a6f1fad8-0` | `datasets/detection_training/export_checkpoint.py:445` |
| `python.lang.security.audit.dangerous-subprocess-use-tainted-env-args` | `feb1c815-fb14-44af-b148-6502a6f1fad8-1` | `datasets/detection_training/export_checkpoint.py:446`, the argv of the same call |
| `python.lang.security.audit.dangerous-subprocess-use-audit` | `69ef51f8-d65b-4291-a4ae-d447bbd2367d-0` | `src/backend/workflow_engine/python_bridge.py:1043` |
| `python.lang.security.audit.dangerous-subprocess-use-audit` | `24e13eb5-0be6-4586-abc1-9ca6aa3b9aab-0` | `src/backend/utils/utils.py:157` |

All three calls already pass an argument list without a shell (Requirement 7.1). In each, the externally influenced values can't be read as options: they follow `-c`, follow `--`, or are option-arguments (Requirement 7.3). Each call's gap is Requirement 7.2: an externally influenced value reaches the call without an allow-list or strict-format check.

### R7 externally influenced arguments

This table satisfies Requirement 7.4 and matches the `external_inputs` of the ledger entries.

| Call | Argument | Source | Externally influenced | Check after this design |
|---|---|---|---|---|
| `export_checkpoint.py:445-447` | Program (`argv[0]`) | `ORT_FLOOR_PYTHON` environment variable (line 70) | Yes | The variable is no longer read; the program is a literal |
| | `-c`, `_FLOOR_SCRIPT` | Literal, and a module constant (line 412) | No | None needed |
| | ONNX, feeds and results paths | Under `WORK`, from `EXPORT_WORK_DIR` (line 69) | Yes | Must be absolute. They are positional after `-c` and read from `sys.argv[1:4]` (line 416) |
| `python_bridge.py:1043-1045` | Interpreter | `sys.executable` (line 997); no builder passes another | No | None needed |
| | `RUNNER_SOURCE` | Module constant (line 545) | No | None needed |
| | Handler path | The compiled document's handler-path argument (line 1339), joined to the artifact directory (lines 1472, 1539) | Yes | The resolved path must stay inside the artifact directory. It is positional after `-c` and read as `sys.argv[1]` (line 812) |
| `utils.py:157`, through `chown`, `chmod` and `chgrp` | Path operand | Folder image-source location (device API), workflow id (device shadow), internal capture paths | Yes | The resolved path is confined below `/aws_dda`, outside `/aws_dda/greengrass` and `/aws_dda/system`. It stays after `--`, as today |
| `utils.py:157`, through `useradd` and `groupadd` | `--uid` and `--gid` values | `DDA_*_USER_ID` and `DDA_*_GROUP_ID` environment variables | Yes | Decimal digits only |
| | User and group names | Constants | No | Existing allow-list `^[a-z_][a-z0-9_-]*$` |
| | `chmod` mode | Constant `770` | No | Existing allow-list |

### R7 detector export floor run

`export_checkpoint.py` is the entry point of the detector export image (`edge-cv-portal/detector-export-image/Dockerfile:65`, `:69`), which the Portal runs as a SageMaker job.

- Line 70 reads the floor interpreter from `ORT_FLOOR_PYTHON`, defaulting to `/opt/ort-floor/bin/python`. The Dockerfile sets the variable to that same default (line 38).
- Lines 445-447 run `[ORT_FLOOR_PYTHON, "-c", _FLOOR_SCRIPT, onnx_path, feeds, results]`, with the paths under `WORK` (line 69).
- The Portal builds the job environment from a fixed key set that sets neither variable (`edge-cv-portal/backend/layers/shared/python/detector_conversion.py:497-505`).

Only a principal that can start training jobs with its own environment could change these values, and that principal could already choose the image. So this isn't exploitable through the product.

The change:

1. Delete line 70. The call's first element becomes the literal `"/opt/ort-floor/bin/python"`: the venv the Dockerfile creates (line 50) and checks at build time (line 67). Removing the read is chosen over validating it:
   - Nothing sets the variable to any other value.
   - Removal takes the environment value out of the program position, while an `if` check isn't a sanitizer the taint rule recognizes.
   - A list whose program is a literal is the form the audit rule doesn't report. The triage saw such lists go unreported in this export.
2. At the top of `run_on_fleet_floor` (line 435), require `WORK` and `onnx_path` to be absolute. If either isn't, call `fatal("EXPORT_WORK_DIR must be an absolute path")` or `fatal("the exported ONNX path must be absolute")`.
3. In the Dockerfile, drop `ORT_FLOOR_PYTHON=/opt/ort-floor/bin/python` (line 38) and the line continuation before it on line 37.

A check failure is fatal for the job. `fatal` raises `SystemExit("FATAL: ...")` (line 164), which takes the same path as every other `FATAL:` check in the module. The job records it as its failure reason (`write_failure`, line 170), the Portal shows that reason for the conversion, and nothing is retried.

The image is pinned by digest, so the fix ships only when the image is rebuilt with `build-and-push.sh --push` and the printed digest is deployed. The digest goes through the `detectorExportImage` context or the SSM parameter (`edge-cv-portal/infrastructure/lib/context-helpers.ts:81-118`). Until then, jobs run the old image. The interpreter, argv and outputs are unchanged.

Tests, in `edge-cv-portal/backend/tests/test_export_checkpoint_static.py`:

- `test_fleet_floor_subprocess` (line 640) stops monkeypatching `ec.ORT_FLOOR_PYTHON` and wraps `subprocess.run` instead. The wrapper asserts that `argv[0] == "/opt/ort-floor/bin/python"`, then runs the fake floor script with `argv[1:]`. All four modes keep their assertions.
- New: a relative `WORK` fails with a message naming `EXPORT_WORK_DIR` and never calls `subprocess.run`. A relative ONNX path fails the same way.
- New, static: no `os.environ` or `os.getenv` read of `ORT_FLOOR_PYTHON` remains, and the `subprocess.run` argv starts with a string literal.

Both findings are expected to close on rescan, though for the taint finding `-1` that is unverified ([Expected rescan results](#expected-rescan-results)). `WORK` stays configurable. If `-1` remains, it is closed as `already-mitigated` rather than by removing the `EXPORT_WORK_DIR` override, which would change the export job's configuration only to satisfy a scanner.

### R7 custom Python handler path

The handler path comes from the handler-path argument of an `emlpython` element in the compiled workflow document (`bridge_specs`, line 1339). `build_bridges` (line 1472) and `build_producer_bridge` (line 1539) join it onto the component artifact directory. The only check before `Popen` is `os.path.isfile` (line 1014), so an absolute or `..` value would make any existing `.py` file on the device the handler.

The Portal compiler and the device planner both emit `python/<node id>/handler.py` (`workflow_core/compiler/compiler.py:818`, `src/backend/workflow_engine/python_source.py:132`). The triage found no node-id format check in `workflow_core`. Exploitability is low: a workflow author can already run arbitrary Python through custom nodes by design (Requirement 8.1), and the document arrives inside the deployed component artifact.

The change adds `_artifact_handler_path(node_id, artifact_path, relative)` to `python_bridge.py` and uses it at both join sites:

- A missing `relative` keeps today's errors (lines 1462-1468 and 1532-1536). An empty `artifact_path` raises `CustomPythonNodeError(node_id, "component artifact directory is unknown")`.
- It computes `joined = os.path.join(artifact_path, relative)`, then resolves both `joined` and `artifact_path` with `os.path.realpath`. The resolved handler must lie strictly inside the resolved root: `os.path.commonpath([root, resolved]) == root`, and `resolved != root`. Otherwise it raises `CustomPythonNodeError(node_id, "handler path '<relative>' resolves outside the component artifact directory")`.
- It returns `joined`, not the resolved path, so valid documents produce the same path string as today. Existing tests assert that string (`test/backend-test/workflow_engine/test_workflow_python_bridge.py:363-369`, and `test_workflow_python_bridge_producer.py:260-267` and `:322-330`).

Containment is chosen over a strict check for the `python/<id>/handler.py` format, for two reasons:

- Containment is the property that matters.
- A format check would tie the device engine to the compiler's layout, including older vendored `workflow_core` copies.

The two builders own the invariant, because they are the only production constructors (`pipeline_executor.py:3254` and `:3381`). The `CustomPythonBridge` constructor stays permissive for tests that build bridges directly.

The error is raised before any process starts, and it fails only that run:

- On the per-frame path, the executor logs it at ERROR and finishes the run as failed, with `failing_node_id` set (`pipeline_executor.py:2057-2091`).
- On the producer path, the executor does the same (lines 1645-1662).

Tests:

- New: `test/backend-test/workflow_engine/test_python_bridge_handler_containment.py`.
  - `python/n1/handler.py` is accepted and returns the same joined string.
  - `../../etc/x.py`, `/usr/lib/x.py` and `python/../../x.py` are rejected, and the error names the node.
  - A symlink inside the artifact directory that points outside it is rejected. One that points inside is accepted.
  - The producer builder gets the same cases, with a `SimpleNamespace` feed.
- Property (Hypothesis): for relative paths built from the segments `..`, `.`, `python`, `n1`, `handler.py` and `/`, a builder either raises `CustomPythonNodeError` or returns a path whose real path is inside the artifact root.
- Existing: `test_workflow_python_bridge*.py` and `test_property_python_source_explicit_caps.py`.

On rescan, the audit rule will still report line 1043, because the program is `sys.executable`, not a literal. That rescan finding is triaged under Requirement 17.6 as `FALSE_POSITIVE` / `already-mitigated`, citing `_artifact_handler_path`.

### R7 DDA permission walk

`run_command` (`utils.py:156-160`) runs an argv that its callers build. All twelve call sites are in `user_group_management_utils.py` and `filesystem_management_utils.py`. Existing controls already stop option injection:

- User and group names are allow-listed.
- `chmod` modes are allow-listed.
- `chown`, `chmod` and `chgrp` put the path after `--` (`filesystem_management_utils.py:66-96`).

The gap is the path operand. `create_dda_user_directory` (`dda_user_management_utils.py:84-101`) creates the directory, then chowns it to the DDA admin user and chmods it to 770. It does the same to every parent except `/` and `/aws_dda`, comparing unnormalized strings. Two external sources reach it:

- A Folder image source's location in the device API body (`resources/accessors/image_source_accessor.py:92` on create, `:209` on update). It is checked only for being absolute (lines 437-440).
- A workflow id from the device shadow's desired streams (`dao/iotshadow/CloudIoTShadowAccessor.py:75` and `:89`). It is appended to `/aws_dda/inference-results/` (`resources/accessors/workflow_accessor.py:298-300`).

A location such as `/etc/x`, or an id containing `../..`, makes the walk chown and chmod system directories inside the container. The container bind-mounts the host's `/aws_dda` and `/tmp` (`src/docker-compose.yaml:69-77`), so a location under `/tmp` would also set the host's `/tmp` to 770. Separately, the `--uid` and `--gid` values come from deploy-time environment variables (`dda_user_management_utils.py:62-65`) with no format check.

The change:

1. Add `confine_dda_path(path) -> str` to `dda_user_management_utils.py`:
   - It raises `TypeError` when `path` is `None`, empty or not a string. This keeps the existing "Folder path is required" handling (line 97).
   - It raises `ValueError` when `path` isn't absolute or contains a NUL.
   - It resolves `path` with `posixpath.realpath`. The result must lie strictly below `posixpath.realpath(constants.DDA_ROOT_FOLDER)`. It must also lie outside `/aws_dda/greengrass` and `constants.DDA_SYSTEM_FOLDER` (`/aws_dda/system`), including everything below them. Otherwise it raises `ValueError` naming the path and the rule.
   - It uses `posixpath` rather than the module's `os` name, because the existing tests replace that name (`test/backend-test/utils/test_dda_user_management_utils.py:96-108`).

   The two subtrees are excluded because they stay root-owned by design. The installer leaves the Greengrass root, `/aws_dda/greengrass`, out of its DDA admin chown (`station_install/setup_station.sh:1212-1219`). `/aws_dda/system` holds scripts that host services run (`src/host_scripts/install_nvidia_csi_service.sh:43-45` and `:70-72`).
2. `create_dda_user_directory` calls `confine_dda_path` before `os.makedirs`, creates the resolved path, and walks only the parents strictly below the root. For valid paths, that is the same set today's skip of `/` and `/aws_dda` produces. It returns `folder_path` unchanged, so the `workflowOutputPath` that `workflow_accessor.py:89` stores keeps today's string. A new `except ValueError` branch logs at ERROR and re-raises, like the existing `OSError` branch (line 94).
3. `update_dda_user_file_permissions` (line 72) confines `filepath` and passes the resolved path to `chown` and `chmod`. Its internal callers already pass paths under `/aws_dda` (`gstreamer/gst_pipeline_executor.py:88` and `:128`).
4. In `user_group_management_utils.py`, `create_user` (line 80) and `create_group` (line 157) check the id inside their existing `if userid:` and `if groupid:` branches (lines 90 and 164). There, `userid` or `groupid` must match `^[0-9]{1,10}$`, or the function raises `ValueError` in the style of `_require_posix_name`. `None` and `""` skip the branch, so the user or group is created without a fixed id, as today. An id is empty whenever the host lookup in `src/host_scripts/setup_dda_users.sh:147-150` returns nothing, because `src/docker-compose.yaml:80-83` passes the variables through as written.
5. `image_source_accessor.__create_folder` (line 435) turns a `ValueError` from `create_dda_user_directory` into a `ValidationError`. The existing handlers return that as HTTP 400 (lines 127-130 and 222-225).
6. `utils.py` doesn't change. The sink is generic, and only the callers know what each argument means, so the checks belong in the callers. A clean rescan here would mean replacing `run_command` with per-tool calls whose program is a literal, at all twelve call sites. That adds no protection, so it isn't proposed.

The allowed area matches how Folder sources are created today:

- The device web UI prefixes every Folder location with `/aws_dda/` (`src/frontend/src/components/image-source/constants.ts:20`, used at `AddImageSource.tsx:118`).
- The workflow catalog's Folder Source node has no default location. Its examples, `/aws_dda/images/latest.jpg` and `/aws_dda/captures`, are under `/aws_dda` (`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py:322-328`), and its location never reaches `create_dda_user_directory`.
- A location is refused only if a direct API call put it outside `/aws_dda`, if it resolves to `/aws_dda` itself (such as a device-UI entry of `.`), or if it lies in one of the two protected subtrees.
- The check runs only when a source is created or updated. Stored sources aren't re-validated, and reading from them doesn't change.

Sources created through the API outside the area are the subject of the Folder image-source roots decision under the [owner decisions](#owner-decisions-before-implementation).

| Condition | Caller receives | Log |
|---|---|---|
| Folder create or update with a location outside the allowed area | HTTP 400 naming the location and the rule; nothing is created | ERROR from `create_dda_user_directory` |
| Shadow workflow id whose results path (`/aws_dda/inference-results/<id>`) resolves outside the allowed area: outside or to `/aws_dda`, or into `/aws_dda/greengrass` or `/aws_dda/system` | `ValueError` from `create_workflow` before the row is written (`workflow_accessor.py:89-90`). It propagates through `_on_accepted` as any create failure does today (`CloudIoTShadowAccessor.py:88-95`), which has no per-id handling. That workflow isn't created, and neither is any desired id after it in the same document. The metadata loop doesn't run, so ids created earlier in the loop get no metadata row. This repeats on every delivery of the document until the id leaves the desired state. An id that leaves the results directory but resolves inside the area, such as `../captures` (`/aws_dda/captures`), is created and its directory walked, as at `4a3f960` (task 2 review) | ERROR from `create_dda_user_directory`, then ERROR `Error occurred: <reason>` from `on_stream_event` |
| Internal capture or failed-image paths | Always inside the area. `pipeline_executor.py:1040-1058` already catches a failure and falls back to `os.makedirs` | Existing |
| A uid or gid that is set but isn't 1 to 10 decimal digits, at container start | `ValueError` from `setup_dda_users_and_groups` (`app.py:265`). Startup fails, as it does today when `useradd` rejects the value. Fatal. An empty or unset id isn't checked, and startup continues as today | Startup traceback |

Tests:

- `test/backend-test/utils/test_dda_user_management_utils.py`: the existing tests stay as they are. New cases:
  - `/etc/x`, `/aws_dda/../etc`, `/aws_dda/inference-results/../..`, `/tmp/images`, `/aws_dda/greengrass/v2/config`, `/aws_dda/system` and a relative path each raise `ValueError`, and none calls `makedirs`, `chown` or `chmod`.
  - With the root monkeypatched to a temporary directory, a symlink below it that points outside is refused.
  - A valid nested path gets today's walk.
  - `update_dda_user_file_permissions` refuses an unconfined path.
- Property (Hypothesis): for any string, `confine_dda_path` either raises or returns a path strictly below the root and outside the excluded subtrees. `create_dda_user_directory` never calls `update_dda_user_file_permissions` with a path outside that set.
- `user_group_management_utils`: `"1001 "`, `"-1"`, `"--help"` and `"abc"` raise `ValueError` without calling `run_command`. `"1001"` keeps today's argv, and so do `""` and `None`, which add no `--uid` or `--gid`.
- `test/backend-test/resources/test_image_source_accessor.py`: creating or updating a Folder source at `/etc/x` returns 400. The existing tests mock `__create_folder` and stay as they are.
- Existing: `test_workflow_accessor.py`, and the `camera_sync` tests that patch `create_dda_user_directory`.

On rescan, the audit rule will still report `utils.py:157`, because its argv is a variable. That finding is triaged under Requirement 17.6 as `FALSE_POSITIVE` / `already-mitigated`, citing `confine_dda_path` and the uid and gid check.

### R7 observations

This spec doesn't fix these:

- `chown` and `chmod` follow symlinks. The DDA admin user owns the 770 directories, so it could swap a checked directory for a symlink between the check and the call. That requires local access to the host. `chown -h` plus a walk based on file descriptors would close the gap.
- The installer runs `chown -R` and `chmod -R 770` over every top-level `/aws_dda` directory except `greengrass` (`setup_station.sh:1212-1219`, `src/host_scripts/setup_dda_users.sh:158-167`). On a re-run, that also covers `/aws_dda/system` if the directory already exists.
- Top-level files below `/aws_dda` stay inside the allowed area, while the installer's loop (`find -maxdepth 1 -type d`) covers only directories. `/aws_dda/authorization_settings.json` (`constants.AUTHORIZATION_SETTINGS_FILE`) is one: its presence turns token authorization on (`utils.is_authorization_enabled_on_station`, an `is_file()` check). A Folder source at that location makes the walk chown the file to the DDA admin user and set 770 when it exists; when it doesn't, `os.makedirs` creates a directory with that name, and authorization can't be turned on until someone removes it. Creating a source needs device API access, which needs a token once authorization is on. `4a3f960` did the same and more, so it isn't a regression. Excluding that file, or every non-directory, from the walk is the owner's call (task 2 review); the file's mode on a device wasn't checked.

## r9-url-fetching

This section remediates two findings (Requirement 9):

| Rule | finding_id | Location |
|---|---|---|
| `B310` | `f035ce5a-9b67-4aac-9ec0-b5f597f0fdcc-0` | `src/backend/workflow_engine/payload_fetch.py:301` |
| `B310` | `95141da5-9489-42a9-a8d5-e066182a8f68-0` | `test/on-hardware/register_vllm_models.py:128` |

### R9 payload reference fetch

`payload_fetch.py` fetches a `bedrock_inference` node's Payload_Reference in the LocalServer workflow engine. The value is resolved from the run's trigger payload, which is untrusted MQTT input, and fetched through `output_bindings.py:2317-2333`.

These controls are already in place:

- The dispatcher (lines 236-245) routes values by prefix:
  - `http://` and `https://` go to `urlopen`.
  - `s3://` goes to boto3.
  - `file://` goes to the confined reader.
  - `data:` and bare base64 are decoded locally.
- Every other scheme falls through to the base64 decoder and fails with a message that doesn't echo the value (lines 489-501). That includes `ftp://`, custom schemes, and uppercase `HTTP://`, because the prefix match is case-sensitive.
- `file://` reads need an explicit `file://` allow-list entry, are confined with `realpath`, and read only regular files (lines 410-465). Requirement 9.2 is met.
- `allowed_uri_prefixes` is checked before the first request (lines 241 and 267-288).
- Fetches have a size cap and a timeout (lines 105, 110) and verify TLS against `certifi` (lines 68-99).

What's missing:

1. Redirect checks (Requirements 9.1 and 9.3). `urlopen` uses the default opener, which follows redirects to `http`, `https` and `ftp` targets and includes `FTPHandler`, `FileHandler` and `DataHandler`. It re-checks neither the scheme nor the prefixes, so an allowed `https://` URL can redirect to plain `http`, to a host outside the allow-list, or to `ftp`. This was confirmed against the standard library locally.
2. Credential redaction (Requirement 9.4). Rejection and fetch errors (lines 282, 309 and 317) carry the whole URL. So does the INFO run-log line (`describe_reference_source`, lines 201-207, logged at `output_bindings.py:2319-2322`). That includes userinfo and the query string, where presigned-URL signatures and session tokens travel. urllib never sends URL userinfo as credentials, so such URLs never fetched. For some forms, though, the `http.client` error text echoes the password (`nonnumeric port: '<password>@host'`, reproduced locally).
3. Tests (Requirement 9.5). No tests cover unsupported schemes, redirects or redaction.

The change uses only the Python standard library:

1. A gated opener. `_build_opener(allowed_prefixes)` returns an `OpenerDirector` with only these handlers:
   - `ProxyHandler()`, so `http_proxy` and `https_proxy` keep working.
   - `UnknownHandler`, `HTTPHandler` and `HTTPSHandler(context=https_ssl_context())`.
   - `HTTPDefaultErrorHandler`, `_GatedRedirectHandler(allowed_prefixes)` and `HTTPErrorProcessor`.

   There is no FTP, file or data handler. `_fetch_http(source, allowed_prefixes)` calls `_build_opener(allowed_prefixes).open(source, timeout=REFERENCE_FETCH_TIMEOUT_SEC)`. Its status and size logic is unchanged.
2. Per-hop checks. `_GatedRedirectHandler` subclasses `urllib.request.HTTPRedirectHandler` and checks each redirect twice, before delegating to the standard implementation:
   - `http_error_302` checks the scheme first. It reads the target the way the standard library does: the first `Location` header, else `URI`. If the target's scheme, parsed with `urlsplit` and lowercased, is neither empty nor `http` or `https`, the redirect is refused. This check can't wait for `redirect_request`: for any scheme other than `http`, `https`, `ftp` or empty, the standard `http_error_302` raises its own `HTTPError`, whose text contains the raw target, before it calls `redirect_request`.

     The class also binds `http_error_301`, `http_error_303` and `http_error_307` to this override, and `http_error_308` where the base class defines it (CPython 3.11; 3.10 doesn't follow 308). The base class binds those names to its own `http_error_302`, so overriding that method alone would leave them unchecked.
   - `redirect_request` then checks the absolute target:
     - The target scheme, parsed with `urlsplit` and compared case-insensitively, must be `http` or `https`.
     - An `https` request may not redirect to `http`.
     - The target may not carry userinfo.
     - `_check_allowed(newurl, allowed_prefixes)` must pass. The rule is the same as for the first hop: an empty list allows any remote source.

   On refusal, either method closes the redirect response and raises `PayloadReferenceError` naming the rejected scheme or the redacted destination. `_fetch_http` already re-raises `PayloadReferenceError` unchanged (lines 313-314). urllib's limits of 10 redirects and 4 repeats stay.
3. Userinfo. An `http(s)` URL with userinfo is refused before any request, with `reference fetch denied: URLs with embedded credentials are not supported (scheme 'https', host '<host>')`.
4. Redaction. `_redact(uri)` returns `scheme://host[:port]/path`. It drops userinfo and the fragment, and replaces any query with `?<redacted>`. A URI that can't be parsed becomes `<unparseable URI>`.
   - Every `PayloadReferenceError` that names a URI uses it: through `_quote`, and through the direct format calls in `_check_allowed`, `_fetch_http`, `_fetch_s3` and `_fetch_file`.
   - `describe_reference_source` uses it too.
   - Exception text appended after `could not fetch reference` has the URL's password and query removed.
5. Unchanged: the dispatcher, S3, `file://` confinement, `data:` and base64 decoding, the size cap, the timeout and the TLS context. The rule that empty `allowed_uri_prefixes` allows any remote source also stays. Default-deny would break nodes configured without prefixes, so the [owner decisions](#owner-decisions-before-implementation) accept this rule as residual risk.

Each failure becomes the node's error outcome: `output_bindings.py:2324-2333` returns the message, and the run handles it as it handles today's payload-reference failures. Nothing new is logged, and the existing INFO line is redacted.

| Condition | Result |
|---|---|
| Unsupported initial scheme | As today: a generic message, no echo, no I/O |
| URL with userinfo | `PayloadReferenceError`, no I/O |
| Initial URL outside the prefixes | `PayloadReferenceError` naming the redacted URL, as today |
| Redirect, with any status the runtime follows, to a scheme other than `http(s)` (including `ftp`, `file`, `gopher` and custom schemes), an `https` to `http` downgrade, userinfo, or a target outside the prefixes | `PayloadReferenceError` naming the rejected scheme or redacted destination. The target isn't requested, and its raw text never reaches the message |
| Redirect loop or too many redirects | `could not fetch reference '<redacted>': HTTP Error 3xx ...` |
| Network error, HTTP error status or timeout | As today, redacted |

`http(s)`, S3, `file://`, `data:` and base64 references work as before. Redirects that stay on `http(s)` and inside the allow-list, such as S3 regional redirects, are still followed, and proxies are still honored. These are the deliberate changes (Requirement 16.6). The ledger entry records part of the first two today, and the `deliberate_changes` follow-up completes it ([Ledger follow-ups](#ledger-follow-ups)):

- URLs are redacted in errors and in the run log.
- Redirects that leave `http(s)` or the allow-list, or downgrade from `https`, are refused.
- URLs with userinfo are refused. Such URLs never fetched anyway.

Tests go in `test/backend-test/workflow_engine/test_payload_fetch.py`, which already serves files from a local `ThreadingHTTPServer` (line 86):

- New: `ftp://`, `gopher://`, uppercase `HTTP://` and `javascript:` are rejected without echo (Requirement 9.5).
- A redirect handler returns 302 with a configurable `Location` and records each requested path. With it:
  - Redirects to `ftp://`, `file://` and `gopher://` are refused, and their targets are never requested. Each refusal is a `PayloadReferenceError` that names the scheme and contains neither the target's path nor a visibly fake query placed on it. The `file://` and `gopher://` cases run for 301, 302, 303 and 307, and for 308 where the runtime follows it, which shows the override is bound for every status.
  - A redirect inside the allowed prefix is followed and returns the bytes.
  - A redirect outside the allowed prefix is refused, and its path is never requested.
  - With no prefixes, a same-host `http` redirect is followed.
- Downgrade: a unit test calls `_GatedRedirectHandler.redirect_request` with an `https` request and an `http` target, and expects a refusal. No local TLS server is needed.
- Opener shape: no `FTPHandler`, `FileHandler` or `DataHandler`, and the `HTTPSHandler` carries `https_ssl_context()`.
- Redaction:
  - A URL with userinfo is refused, and the message contains neither the user nor the password.
  - A 404 on a URL whose query holds visibly fake `X-Amz-Signature` and `X-Amz-Security-Token` values reports the path but not those values.
  - `describe_reference_source` drops the query.
- Property (Hypothesis): for URLs with generated userinfo and query values, neither appears in any `PayloadReferenceError` message or in `describe_reference_source` output.
- Updated: `test_http_fetch_passes_bounded_timeout` and `test_http_timeout_error_names_source` (lines 389-436) patch `payload_fetch._build_opener` instead of `urllib.request.urlopen`. The timeout and source assertions stay. The `context` assertion moves to the opener-shape test.
- Existing tests that assert `url in message` stay valid, because their URLs have no userinfo or query. Also run `test_bedrock_payload_reference.py`.

`urlopen` is no longer called, so this `B310` finding is expected to close on rescan.

### R9 vLLM registration script

`test/on-hardware/register_vllm_models.py` is Shipped_Code, because `README.md:900-905` tells operators to run it.

- It builds every request URL from `--portal-api` or `PORTAL_API` (lines 141-143, 167 and 250).
- It sends `Authorization: Bearer <token>` with each request (line 122).
- It never checks the scheme, so an `http://` value would send the token in clear text.
- `urlopen` (line 128) follows redirects, and urllib copies every non-content header, including `Authorization`, onto the redirected request. A redirect would hand the token to its target.

The change:

- `_validate_portal_api(value)` parses the value with `urlsplit`. It requires the scheme `https`, compared case-insensitively, and a host. It rejects userinfo, a query or a fragment. It returns the value with any trailing `/` removed.

  `main` calls it right after the missing-settings check (line 247) and passes any failure to `parser.error`. The message names only the rejected scheme or the rule, for example `--portal-api / PORTAL_API must be an https:// URL (got scheme 'http')`. It never includes the URL or the token (Requirement 9.4). `--dry-run` returns before this check, as it does today.
- No redirects. `_NoRedirect` subclasses `HTTPRedirectHandler` and returns `None` from `redirect_request`. The script builds `_OPENER = urllib.request.build_opener(_NoRedirect())`, and `_request` calls `_OPENER.open(req, timeout=timeout)`. A 3xx response then arrives as `HTTPError`, which the existing handler (line 130) returns as `(status, payload)`. `main` reports it as `listing models failed: HTTP 302 ...` and exits 1.

An invalid base URL exits with argparse's code 2 and that message. The documented usage, an `https` API Gateway URL that never redirects, is unchanged.

Tests: a new `test/backend-test/security/test_register_vllm_models_portal_api.py` loads the script from `test/on-hardware/` with `importlib`.

- `http`, `ftp`, a missing scheme, userinfo and a query each give `SystemExit(2)`. Stderr names the scheme or the rule and contains neither the token nor the URL.
- `https` and uppercase `HTTPS` are accepted. `_request` is monkeypatched, so there is no network access.
- A local HTTP server answers 302 toward a recording path. `_request` returns 302, and the recording path never receives the `Authorization` header.
- `--dry-run` still works with no settings.

`urlopen` is gone, so this `B310` finding is expected to close on rescan.

### R9 observations

This spec doesn't fix these:

- `_fetch_http` inside `HELPERS_SOURCE` (`python_bridge.py:423-449`) has the same redirect and echo pattern. It belongs to the `dda_frames` helpers that custom Python nodes call. Bandit can't see it, because the code is a string. Handler code is user-authored and can fetch any URL itself (Requirement 8.1). Even so, well-behaved handlers rely on `dda_frames`' prefix gate for URLs supplied in payloads, so applying the same gated opener there is a natural follow-up.
- `src/backend/healthcheck.py:61` also calls `urlopen`, on fixed loopback URLs, and carries `# nosec B310`. That marker explains its absence from the export ([What the scanning platform honors](#what-the-scanning-platform-honors)), so it isn't expected on rescan unless the platform stops honoring Bandit markers.

## r10-deserialization

This section remediates six findings, three on each of two byte-identical copies of `reference_image_map_migration.py` (Requirement 10):

| Rule | finding_id | Copy | Line |
|---|---|---|---|
| `B403` | `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-4` | `src/backend/lyra_science_processing_utils/model_processors/` | 60 |
| `B301` | `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-3` | Same | 68 |
| `python.lang.security.deserialization.avoid-dill` | `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-5` | Same | 68 |
| `B403` | `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-1` | `edge-cv-portal/test-sandbox/dda_triton_resources/lyra_science_processing_utils/model_processors/` | 60 |
| `B301` | `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-0` | Same | 68 |
| `python.lang.security.deserialization.avoid-dill` | `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-2` | Same | 68 |

### R10 current behavior

The module is an offline migration CLI for legacy reference-image maps, run by an operator.

- `main` (lines 73-93) takes a path from argv and only checks that it exists (line 86).
- `migrate_legacy_map` imports `dill` locally (line 60), opens the path (line 65) and calls `dill.load` (line 68). The `# nosem` comments on lines 60 and 68 didn't suppress the platform findings.
- It writes only JSON and `np.save(..., allow_pickle=False)` (`reference_image_map_io.py:90-94`), so the no-new-pickle clause of Requirement 10.2 is already met.

The inference path never imports the module. `SupervisedBBoxStage1PostProcessor` loads only the JSON and NumPy sidecars (`supervised_bbox_stage1_postprocessor.py:57-78`, through `reference_image_map_io.py:98-110`). When the sidecars are missing, its error message tells the operator to run this CLI on the model's legacy map. Its own comment says the map may be supplied externally.

The two copies ship differently:

- The `src/backend` copy ships in every LocalServer image. Each one copies `lyra_science_processing_utils`: `src/backend/Dockerfile:127`, `Dockerfile.jp5:235`, `Dockerfile.jp6:447`, `Dockerfile.jp7:591` and `Dockerfile.x86_64_nvidia:144`. Those images install `dill` (`src/backend/edge_ml1_p_camera_management/install_edgemlsdk.sh:26`).
- The sandbox copy sits in a tracked staging tree, which the copy commands in `edge-cv-portal/test-sandbox/README.md:129-139` fill from `src/backend`. At `4a3f960` the module and `reference_image_map_io.py`, its one import from the package, are byte-identical to their sources. The staging tree as a whole isn't: five other files lag their sources ([R10 observations](#r10-observations)). The tree ships in the Portal's workflow test-sandbox image (`edge-cv-portal/test-sandbox/Dockerfile:93`), which is built out of band. The sandbox doesn't install `dill`, so the CLI can't load a map there.

The gap (Requirements 10.1 and 10.2): nothing checks where the file lives or who can write it. The only trust boundary is the module docstring.

Two facts about devices shape the check:

- `/aws_dda` is owned by `dda_system_user:dda_system_group` with mode 775. Every top-level directory under it except `greengrass` is set recursively to DDA admin ownership and mode 770 (`station_install/setup_station.sh:1207-1219`, `src/host_scripts/setup_dda_users.sh:153-167`). A rule that every parent directory must be non-group-writable would therefore refuse everything under `/aws_dda`.
- Files the DDA admin user can write are group-writable.

### R10 change

Add `open_trusted_legacy_map(path)` to `reference_image_map_migration.py`. `migrate_legacy_map` calls it first, imports `dill` only after it returns, and loads from the handle it returns. The function:

1. Resolves the path: `resolved = os.path.realpath(path)`.
2. Checks the location. `resolved` must lie strictly below one of `TRUSTED_LEGACY_MAP_ROOTS`, a module constant with two entries:
   - `/aws_dda/greengrass/v2/packages/artifacts-unarchived` holds component artifacts. Only the Greengrass nucleus writes there, from verified deployments.
   - `/aws_dda/dda_triton/triton_model_repo` is the Triton model repository that the device converts deployed models into (`src/backend/dda_triton/constants.py:44`).

   The constant is local to the module. `lyra_science_processing_utils` also ships in the sandbox and the Triton python backend, so it mustn't import the backend's `utils.constants`.
3. Opens the file with `os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)`. A symlink swapped in after step 1 fails with `ELOOP`.
4. Checks what was opened. `os.fstat` on the descriptor must show a regular file owned by uid 0 or the effective uid, with neither `S_IWGRP` nor `S_IWOTH` set.
5. Returns `os.fdopen(fd, "rb")`.

Both location and ownership are checked, because each alone leaves a gap:

- Ownership alone admits root-owned files that a root process wrote on a user's behalf. The backend runs as root.
- Location alone admits group-writable files in the model repository, on stations where the installer's 770 pass covered `/aws_dda/dda_triton`.

Checking the opened descriptor, not the path, means the bytes `dill` reads are the bytes that were checked. `migrate_legacy_map` owns the invariant, because it holds the only `dill.load` call.

A refusal raises `UntrustedLegacyMapError`, a `ValueError` subclass, naming the path and the failed condition:

- Outside the roots.
- Not a regular file.
- Owned by uid N.
- Group- or world-writable.

The message adds a fix hint: move the file under a model artifact root, or make it root-owned and not group- or world-writable (`chown root`, `chmod go-w`). Nothing is loaded and nothing is written. `main` passes the message to `parser.error`, which prints it on stderr and exits with code 2. Programmatic callers get the exception.

Outputs are unchanged. `--reference-image-map-file` still chooses the output base, so a model whose configuration points elsewhere can be converted from a file in an allowed location without changing that configuration. The docstring's trust-boundary note is rewritten to describe the enforced check.

The two copies share one fix. The change is made in `src/backend`, and then that one file is copied over the sandbox copy, so the two stay byte-identical. The README's copy commands aren't run. They copy three Triton templates and the whole `lyra_anomalies_mask_utils` and `lyra_science_processing_utils` trees, and five of the staged files they would overwrite lag their sources by about 337 changed lines of inference code ([R10 observations](#r10-observations)). Running them would ship that code in the sandbox image as part of this fix, with no ledger record (Requirement 16.6) and no sandbox verification. The copied file needs no other staged file to change: its direct imports are the standard library, `dill` inside `migrate_legacy_map`, and `reference_image_map_io.py`, which is byte-identical in both trees.

Fixing the sandbox copy is recommended over dropping it:

- A dropped copy would come back with the next full re-stage, because the README's commands copy the whole `lyra_science_processing_utils` tree. Keeping it out would mean changing those commands too. A fixed copy stays fixed through a re-stage, which copies the fixed `src/backend` file.
- The fixed module is inert in the sandbox anyway, because `dill` isn't installed there.
- A parity test on this one file catches drift between the two copies.

The alternative under the [owner decisions](#owner-decisions-before-implementation) is to drop the utility from both trees if no legacy maps remain in use. That would close all six findings on rescan, but the postprocessor's error message would then need a different conversion path.

On rescan, `B301`, `B403` and `avoid-dill` will still report all six lines, because these rules flag the import and the load themselves. Those rescan findings are recorded as `FALSE_POSITIVE` / `already-mitigated`, citing `open_trusted_legacy_map` (Requirements 17.5 and 17.6). Inline suppressions wouldn't help: the platform reported the lines that already carry `# nosem`, and any new suppression needs owner approval anyway (Requirement 18.4).

### R10 preserving function

On a device, converting a deployed model's legacy map from the unarchived artifacts works as before, provided the unarchived file is owned by root or by the user running the CLI and has no group or other write bit. Nobody has checked that owner on a device. Greengrass applies the recipe's artifact permissions, and the owner may be the component's run user instead. In that case the CLI refuses the map with `Owned by uid N`. The device checklist records the owner ([Device and account verification](#device-and-account-verification)), and decision 13 covers admitting that uid.

A map in the Triton repository is refused, with the fix in the message, on stations where the installer's 770 pass covered `/aws_dda/dda_triton`. That refusal is a deliberate change (Requirement 16.6), which the ledger entries gain through the `deliberate_changes` follow-up ([Ledger follow-ups](#ledger-follow-ups)). Inference doesn't change.

As with R7's allowed area, there's no inventory of where stations keep legacy maps. If a station keeps them elsewhere, either the owner widens `TRUSTED_LEGACY_MAP_ROOTS` or the operator moves the file.

### R10 tests

New: `test/backend-test/lyra/test_reference_image_map_migration_confinement.py`, with `TRUSTED_LEGACY_MAP_ROOTS` monkeypatched to temporary directories.

- Round trip, behind `pytest.importorskip("dill")`: a legacy map written with `dill` under the root, owned by the test user with mode 0644, migrates. `load_safe_reference_image_map` reproduces the ordered paths and the `np.vstack` gallery, and `np.load(..., allow_pickle=False)` reads the matrix (Requirement 10.2).
- Refusals. `dill.load` is monkeypatched to record calls, so each case also shows it is never called. The cases are:
  - A file outside the roots.
  - A `..` escape.
  - A symlink inside the root that points outside it.
  - Mode 0664, and mode 0646.
  - A directory, and a FIFO.
- Owner rule: unit tests of the stat check use synthetic `os.stat_result` values for uid 0, the effective uid and another uid, because a test can't `chown` without root.
- CLI: `main()` on a refused path exits with code 2 and names the condition.
- Parity: the two copies of `reference_image_map_migration.py` are byte-identical (`filecmp.cmp(..., shallow=False)`). The test covers only this file, because the rest of the staging tree lags its sources ([R10 observations](#r10-observations)).
- Static: in both copies, `dill` is imported only inside `migrate_legacy_map`, after the call to `open_trusted_legacy_map` (checked by AST order).

Existing: `test/backend-test/security/preservation/test_preservation_deserialization_roundtrip.py`.

### R10 observations

This spec doesn't fix this. The tracked test-sandbox staging tree lags its sources. At `4a3f960`, five files under `edge-cv-portal/test-sandbox/dda_triton_resources/` differ from the files that the README's copy commands take them from, by about 337 lines added or removed in all. Each staged file equals an earlier revision of its source.

| Staged file | Source directory | Lines changed |
|---|---|---|
| `inference_runtimes.py` | `src/backend/dda_triton/resources_for_copy/` | 117 |
| `lyra_science_processing_utils/model_processors/basic_preprocessor.py` | `src/backend/lyra_science_processing_utils/model_processors/` | 102; the source adds the letterbox transform |
| `lyra_science_processing_utils/model_processors/yolo_detection_postprocessor.py` | Same | 58 |
| `marshal_for_capture_template.py` | `src/backend/dda_triton/resources_for_copy/` | 44 |
| `lfv_model_template.py` | Same | 16 |

The rest of the staging tree matches its sources, including `reference_image_map_migration.py`, `reference_image_map_io.py`, `lyra_anomalies_mask_utils` and `ensemble_model`. The sandbox image copies the tracked tree (`edge-cv-portal/test-sandbox/Dockerfile:93`), so the sandbox's model staging runs the older templates and processors. A full re-stage would bring the sandbox in line with `src/backend`. It changes what the sandbox runs, though, so it is a separate change with its own sandbox verification, and [R10 change](#r10-change) copies only the fixed module.

## r12-identifier-hashes

This section remediates two findings (Requirement 12):

| Rule | finding_id | Location |
|---|---|---|
| `B324` | `e1583a8e-82f5-468a-829e-9cc91e0e5bb7-0` | `src/backend/camera_discovery/aravis.py:105` |
| `B324` | `93e57454-5aca-4e33-a9a0-fe6de96905c1-0` | `src/backend/camera_discovery/discovery.py:170` |

### R12 current behavior

Two functions derive camera ids from SHA-1, encoding their input as UTF-8:

- `aravis_stable_id` (`aravis.py:86-106`) returns `arv-` plus the first 12 hex digits of SHA-1 over `vendor|model|serial`. When the serial is empty, it hashes `vendor|model|serial|physical_id` instead.
- `make_stable_id` (`discovery.py:167-171`) returns `disc-` plus the first 12 hex digits of SHA-1 over `bus_info + card_name`.

These ids persist in three places:

- They are reported as `camera_source_id` to the camera registry (`camera_sync/inventory.py:412` and `:431`).
- Workflow bindings reference them.
- `inventory.py:142` derives the static-image camera's id from `aravis_stable_id`.

The hash builds an identifier. Nothing uses it for authentication, tamper detection or password storage, so Requirement 12.1 holds. Changing the algorithm or the truncation would change every existing id and orphan persisted references, so both stay (Requirement 12.2). The gap is Requirement 12.3's non-security marker.

### R12 runtimes

Requirement 12.3 asks for a form that runs on every Device_Runtime that imports these modules, including Python 3.8. Every importer runs inside the LocalServer backend image: `camera_sync`, `utils/server_setup.py` and `workflow_engine/runtime.py`. Each backend Dockerfile copies `camera_discovery`.

| Image | Backend interpreter | Evidence |
|---|---|---|
| Generic (`src/backend/Dockerfile`) | CPython 3.11 | Lines 13-14; `CMD ["python3.11", "app.py"]` at line 128; copy at line 105 |
| JetPack 5 (`Dockerfile.jp5`) | CPython 3.11.9, built from source | `PYTHON_SRC_VERSION=3.11.9` at line 83; lines 93-94; `CMD` at line 236; copy at line 214 |
| JetPack 6 (`Dockerfile.jp6`) | CPython 3.10 | `BACKEND_PYTHON_VERSION` 3.10 (`build-custom.sh:94-98`); lines 44 and 119-120; `CMD` at line 450; copy at line 426 |
| JetPack 7 (`Dockerfile.jp7`) | CPython 3.11 | Lines 120-128 and 195-196; `CMD` at line 594; copy at line 572 |
| x86 NVIDIA (`Dockerfile.x86_64_nvidia`) | CPython 3.11 | Lines 32-33; `CMD` at line 145; copy at line 122 |

No shipped image runs these modules on Python 3.8. On JetPack 5, the host's Ubuntu 20.04 system Python 3.8 runs no LocalServer code. It's used only if someone runs the host-side test suites with that interpreter.

### R12 change

Add `src/backend/camera_discovery/stable_hash.py`:

```python
"""SHA-1 used only to derive stable, non-secret camera ids (Requirement 12)."""
import hashlib


def id_digest_hex(text: str) -> str:
    """Hex SHA-1 of ``text`` (UTF-8), marked as a non-security use."""
    data = text.encode("utf-8")
    try:
        digest = hashlib.sha1(data, usedforsecurity=False)
    except TypeError:
        # Python 3.8 has no usedforsecurity keyword (added in 3.9). Same digest,
        # still an identifier, not a security use.
        digest = hashlib.sha1(data)
    return digest.hexdigest()
```

`aravis.py:105` becomes `digest = id_digest_hex(key)`, and `discovery.py:170` becomes `digest = id_digest_hex(bus_info + card_name)`. Each module drops `import hashlib`, its only use. Each Dockerfile's `COPY camera_discovery` picks up the new module.

The helper tries the keyword and falls back on `TypeError`:

- Requirement 12.3 names Python 3.8, which raises `TypeError` for the keyword.
- A `try` is testable on any interpreter, unlike a `sys.version_info` branch.
- One shared helper leaves a single fallback line.

Bandit clears `B324` only when `usedforsecurity=False` appears on the call itself, so the rescan will report the fallback line. That is one finding instead of two, on a line no shipped image runs. It is triaged under Requirement 17.6 as `FALSE_POSITIVE` / `scanner-misread`: an identifier hash on an interpreter path that no shipped image takes. Alternatively, it is closed with an owner-approved `# nosec B324` (Requirement 18.4).

The owner could instead keep only the keyword call, with no fallback. That gives a clean rescan and is correct on every shipped image. It fails on a Python 3.8 interpreter, though, which Requirement 12.3 names, so it isn't the default.

The change adds no failure modes. A non-string input raises as it does today, and every caller passes strings.

### R12 preserving function

The helper's output is byte-identical to the current derivation. Every persisted `camera_source_id`, registry entry and workflow binding stays valid, and nothing needs migrating. The golden tests below pin this.

### R12 tests

New: `test/backend-test/camera_discovery/test_stable_id_goldens.py`.

Goldens, computed from the unchanged code at `4a3f960`:

| Call | Id |
|---|---|
| `aravis_stable_id("Basler", "acA1920", "12345678")` | `arv-261e4a8e765b` |
| `aravis_stable_id("V", "M", "", "phys-1")` | `arv-31d3ec281d08` |
| `aravis_stable_id("Aravis", "Fake GV Camera", "GV01")` | `arv-68b74f7de25a` |
| `make_stable_id("usb-0000:00:14.0-1", "Cam A")` | `disc-da0f363d2660` |
| `make_stable_id("platform:tegra-capture-vi:0", "vi-output, imx219 9-0010")` | `disc-f8dd2b32a75b` |

Other cases:

- Fallback: `stable_hash.hashlib` is monkeypatched with a stub whose `sha1` raises `TypeError` when given `usedforsecurity` and delegates otherwise. The goldens still hold.
- Property (Hypothesis): `id_digest_hex(s) == hashlib.sha1(s.encode("utf-8")).hexdigest()` for any text.
- Static:
  - An AST check over `camera_discovery` confirms that `hashlib.sha1` is called only in `stable_hash.py`, and that its first call passes `usedforsecurity=False`.
  - `ast.parse(..., feature_version=(3, 8))` runs over `stable_hash.py`, `aravis.py` and `discovery.py`, in the same style as the gate in `test/backend-test/test_py310_compat.py`.

Existing tests must stay green:

- `test_aravis_enumeration.py`, including lines 110-116.
- `test_camera_enumeration.py`, `test_property_aravis_stable_id.py` and `test_discovery_aravis_wiring.py`.
- The camera_sync suites.
- The static-image id pin in `test/backend-test/camera_sync/test_property_static_camera_duplicate_registration.py:337-345`, which compares the derived id with the registry's recorded value.

## r14-sns-encryption

This section remediates the two findings on the training-alerts topic (Requirements 14.1 and 14.2). In this part, `compute-stack.ts` and `usecase-account-stack.ts` are in `edge-cv-portal/infrastructure/lib/`, and line numbers are at `4a3f960`.

| Rule | finding_id | Reported at | Logical id | CDK source | Unfixed_Snapshot twin (`test-only`) |
|---|---|---|---|---|---|
| `CKV_AWS_26` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-7` | `iam_baseline_EdgeCVPortalComputeStack.template.json:43560` | `TrainingAlertTopic5C2CFA97` | `compute-stack.ts:3448-3451` | `950b9d60-0315-4329-9633-02b8aa44298c-10` |
| `scanner-x/sns-topic-encryption` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-0` | same line | same | same | `950b9d60-0315-4329-9633-02b8aa44298c-0` |

Two scanners report the same resource, so one CDK change closes both findings. The topic moves to the AWS managed SNS key, `alias/aws/sns`. If the owner finds an AWS service publishing to the topic from outside the repository, the topic gets a customer managed key instead.

### R14 SNS current behavior

- `compute-stack.ts:3448-3451` creates `dda-portal-training-alerts` with only `displayName` and `topicName`. The topic has no server-side encryption. The template has no `KmsMasterKeyId`, which is the only property either rule reads.
- Two Lambda functions in the same stack publish to it, through `trainingAlertTopic.grantPublish`: `TrainingEventsHandler` (`:3473`) and `CompilationEventsHandler` (`:3497`). They publish only failure alerts (`training_events.py:221` and `:271`, `compilation_events.py:200`). Each publish sits in a `try` block that logs `Error sending SNS notification` at ERROR and continues.
- Nothing in the repository makes an AWS service a publisher. No CloudWatch alarm action, EventBridge `SnsTopic` target, S3 notification or topic policy references the topic. The ARN leaves the stack only through the `TrainingAlertTopicArn` output, exported as `EdgeCVPortalTrainingAlertTopicArn` (`:3946-3950`), and nothing in the repository imports that export.
- CDK defines no subscriptions. Operators add them outside the stack, for example as email subscriptions.

### R14 SNS change

After `compute-stack.ts:3451`, set the key on the L1 resource:

```ts
    // Encryption at rest with the AWS managed SNS key (R14.1). The only
    // publishers are the TrainingEvents and CompilationEvents Lambda roles in
    // this account, which that key's policy admits without a KMS grant. An AWS
    // service publisher (CloudWatch alarm, EventBridge target) can't use the
    // AWS managed key and would need a customer managed key whose key policy
    // admits that service (R14.2).
    (trainingAlertTopic.node.defaultChild as sns.CfnTopic).kmsMasterKeyId = 'alias/aws/sns';
```

Why this form:

- The L2 `masterKey` prop takes an `IKey`, and `kms.Alias.fromAliasName` renders the alias as an ARN built from the stack's partition, region and account. `alias/aws/sns` is the value the AWS::SNS::Topic documentation gives for the AWS managed key, and setting the L1 property emits exactly that literal.
- `topic.masterKey` stays unset, so `grantPublish` adds no KMS statement, and the AWS managed key needs none. No IAM statement changes, so the IAM preservation synth gate's result doesn't move.
- A probe synthesized with the repository's aws-cdk-lib 2.270.0 and scanned with the pinned Checkov 3.2.255 showed that both forms pass `CKV_AWS_26` and that neither adds a KMS statement to the publisher role. In that release, `CKV_AWS_26` accepts any `KmsMasterKeyId` value (`SNSTopicEncryption.py`).

A customer managed key isn't the default. For two same-account publishers it adds a monthly key charge, request charges and a key policy to maintain, without adding access control that the AWS managed key lacks.

Owner check (open question 2). Before the default ships, the owner confirms in each deployed portal account that no AWS service publishes to the topic. These checks are read-only:

- `aws cloudwatch describe-alarms`: the topic ARN appears in no `AlarmActions`, `OKActions` or `InsufficientDataActions`.
- `aws events list-rule-names-by-target --target-arn <topic ARN>` returns no rules.
- `aws sns get-topic-attributes --topic-arn <topic ARN>`: the `Policy` attribute grants `sns:Publish` to no service principal.

If any check finds a service publisher, the topic uses a customer managed key instead (Requirement 14.2):

- `new kms.Key(this, 'TrainingAlertTopicKey', { alias: 'dda-portal/training-alerts', enableKeyRotation: true, removalPolicy: cdk.RemovalPolicy.RETAIN })`, plus a key-policy statement that gives the service principal `kms:Decrypt` and `kms:GenerateDataKey*`, conditioned on `aws:SourceAccount` equal to this account.
- `masterKey: trainingAlertTopicKey` on the topic, in place of the L1 property.
- `grantPublish` then grants `kms:Decrypt` and `kms:GenerateDataKey*` on the key to both publisher roles, as the probe confirmed. That adds two statements to `iam_post_fix_approved_additions.json` (Requirement 15.6).

### R14 SNS preserving function

- `KmsMasterKeyId` on AWS::SNS::Topic updates with no interruption. The topic name, ARN, export and subscriptions stay (Requirement 16.2).
- The publishers are Lambda roles in the topic's own account. The AWS managed key's policy admits principals in that account that call through SNS, so they keep publishing without a KMS grant.
- SNS decrypts each message before delivering it, so existing subscriptions, such as email, keep receiving alerts.
- If the account has never used `alias/aws/sns` in that region, AWS creates the managed key on first use.
- Nothing user-facing changes (Requirement 16.6).

### R14 SNS errors

| Operation | Failure | Recoverable or fatal | Caller receives | Logged |
|---|---|---|---|---|
| Stack update sets `KmsMasterKeyId` | SNS rejects the attribute | Fatal for that deploy. CloudFormation rolls the topic back | A failed deploy | CloudFormation stack events |
| Failure-alert publish | KMS denies or throttles the data-key request | Recoverable. That one alert is lost; job state handling doesn't depend on it | The handler returns 200, as today | ERROR `Error sending SNS notification: <error>` (existing) |
| Publish by an AWS service wired up outside the repository | The AWS managed key doesn't admit service principals | Recoverable by switching to the customer managed key | The service's own delivery error | The service's own history, such as alarm history; nothing in portal logs |

The change adds no external input; the key alias is a literal.

### R14 SNS tests

R14 and R15 share a new Jest file, `edge-cv-portal/infrastructure/test/security-scan-remediation-infra.test.ts`. It synthesizes `StorageStack` and `ComputeStack` once in `beforeAll`, as `user-admin-audit-grant.test.ts` does, and a `UseCaseAccountStack` once, as `camera-shadow-sync-provisioning.test.ts` does. The SNS cases:

- The `dda-portal-training-alerts` topic has `KmsMasterKeyId` `alias/aws/sns`, and its `TopicName` and `DisplayName` are unchanged.
- Every `Ref` to the topic sits in an `AWS::Lambda::Function` environment, an `AWS::IAM::Policy` or `AWS::IAM::ManagedPolicy` document, or the stack output. Any other referencing resource, such as an alarm, an events rule, a subscription or a topic policy, fails the test. The failure message says that an AWS service publisher needs the customer managed key (Requirement 14.2). This test enforces in the CDK layer the precondition the AWS managed key depends on.
- No IAM statement in the stack names `alias/aws/sns`.

Checkov `CKV_AWS_26` must pass on `TrainingAlertTopic5C2CFA97` in the fresh synth and in the refreshed Baseline_Template ([R14 and R15 fixtures and deploy constraints](#r14-and-r15-fixtures-and-deploy-constraints)). `scanner-x/sns-topic-encryption` can't be run locally, because Scanner-X's rule set isn't public. It is expected to clear with the same property, and only the owner's platform rescan confirms that.

### R14 SNS observations

The scan didn't report these, and this spec doesn't fix them:

- `CognitoAdminActivityTopic` (`compute-stack.ts:2294-2297`) has no key, and an EventBridge rule publishes to it (`:2351`). The committed Baseline_Template predates the topic, so the scan couldn't see it. Encrypting it needs a customer managed key whose policy admits `events.amazonaws.com`.
- `BuildAlertTopic` (`build-fleet-stack.ts:197`) has no committed template fixture, so it wasn't scanned.
- `training-workflow-stack.ts:50` defines another training-alerts topic, but no app entry point instantiates that stack.

## r14-sqs-encryption

This section remediates `CKV_AWS_27` on six queues: three pairs of a queue and its dead-letter queue (DLQ), all in ComputeStack (Requirements 14.3 and 14.4). All six are reported in `iam_baseline_EdgeCVPortalComputeStack.template.json`.

| finding_id | Reported at | Logical id | CDK source | Unfixed_Snapshot twin (`test-only`) |
|---|---|---|---|---|
| `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-13` | `:29132` | `CameraShadowReportDLQ50DB798A` | `compute-stack.ts:1672-1676` | `950b9d60-0315-4329-9633-02b8aa44298c-11` |
| `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-8` | `:29180` | `CameraShadowReportQueue78573A06` | `compute-stack.ts:1681-1690` | `950b9d60-0315-4329-9633-02b8aa44298c-12` |
| `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-9` | `:31597` | `AccountSyncAckDLQ75DF4763` | `compute-stack.ts:2047-2051` | `950b9d60-0315-4329-9633-02b8aa44298c-13` |
| `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-10` | `:31645` | `AccountSyncAckQueue772F11D7` | `compute-stack.ts:2056-2065` | `950b9d60-0315-4329-9633-02b8aa44298c-14` |
| `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-11` | `:34238` | `DdaAutolabelDLQB2573578` | `compute-stack.ts:2558-2562` | None; the queue postdates the Unfixed_Snapshot |
| `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-12` | `:34286` | `DdaAutolabelQueue5780A6F7` | `compute-stack.ts:2564-2573` | None |

The default plan has two parts:

- The auto-label pair moves to the AWS managed SQS key, `alias/aws/sqs`, which clears the check.
- The camera-shadow and account-sync-ack pairs take SQS-managed server-side encryption (SSE-SQS), because use-case accounts may send to them. SSE-SQS keeps every sender working, but Checkov 3.2.255 keeps reporting those four queues, so the owner decides how that residual is closed.

### R14 SQS current behavior

None of the six queues sets `encryption`. All set `enforceSSL: true`, so their queue policies deny requests made without TLS, and all have fixed `queueName`s. The template carries neither `KmsMasterKeyId` nor `SqsManagedSseEnabled`. Since late 2022, SQS has enabled SSE-SQS by default on new queues, so the deployed queues are probably encrypted already. Nobody checked that against a live account, and the template doesn't say so, which is what the rule reads.

| Queue (CDK line) | Senders | Consumer |
|---|---|---|
| `dda-portal-camera-shadow-reports` (`:1681`) | The portal-account IoT rule role `CameraShadowRuleRole` (`:1746-1757`, rule at `:1759`). In each cross-account use case, the use-case account's `DDACameraShadowRuleRole` (`usecase-account-stack.ts:904-917`, rule at `:919`). That role holds only `sqs:SendMessage` on the queue ARN; the queue-policy statement `AllowUseCaseAccountIotRuleDelivery` (`:1696-1709`) admits it | `CameraSyncHandler`, through `SqsEventSource` (`:1728`) |
| `dda-portal-camera-shadow-reports-dlq` (`:1672`) | SQS redrive after 3 receives (`:1686-1689`), and `CameraSyncHandler`'s explicit dead-lettering (`grantSendMessages` at `:1735`, `camera_sync.py:948`) | None in CDK |
| `dda-portal-account-sync-acks` (`:2056`) | The portal-account IoT rule role `UserAccountsShadowRuleRole` (`:2090-2101`, rule at `:2103`). The queue policy (`:2071-2084`) also admits every trusted use-case account, although no use-case-side rule in the repository targets this queue | `AccountSyncHandler`, through `SqsEventSource` (`:2148`) |
| `dda-portal-account-sync-acks-dlq` (`:2047`) | SQS redrive (`:2061-2064`), and `AccountSyncHandler` (`:2155`, `account_sync.py:544`) | None |
| `dda-portal-autolabel-queue` (`:2564`) | `DdaLabelingWorker` (`grantSendMessages` at `:2603`, `send_message_batch` at `dda_labeling_worker.py:548`) | `DdaAutolabelWorker`, through `SqsEventSource` (`:2784`) |
| `dda-portal-autolabel-queue-dlq` (`:2558`) | SQS redrive (`:2569-2572`) | None |

No AWS service principal, such as SNS, EventBridge or S3, sends to any of the six. The IoT rule actions send through their IAM roles.

### R14 SQS encryption choice

| | SSE-SQS (`sqs.QueueEncryption.SQS_MANAGED`) | AWS managed key (`QueueEncryption.KMS_MANAGED`) | Customer managed key (`QueueEncryption.KMS` with `encryptionMasterKey`) |
|---|---|---|---|
| Template | `SqsManagedSseEnabled: true` | `KmsMasterKeyId: alias/aws/sqs` | `KmsMasterKeyId` set to the key's ARN |
| KMS permissions for senders and consumers | None | None for principals in this account. The key's policy admits them through SQS, and CDK adds no grant | An IAM grant on the key for each. `grantSendMessages` and `SqsEventSource` add them; hand-written `sqs:SendMessage` statements, such as the IoT rule roles', need explicit grants |
| Senders in another account | Work | Blocked: an AWS managed key can't be used from another account | Need a key-policy statement for that account and a KMS grant in the sender's own account |
| `CKV_AWS_27` in Checkov 3.2.255 | Fails | Passes | Passes |
| Cost and resources | None | KMS request charges | A monthly key charge, request charges, and a key resource (plus an alias) |

How the Checkov behavior was verified. Checkov 3.2.255 is the version `ledger.json` records in `scanner_versions`. On the triage host it is installed in a virtualenv outside the repository, written `<triage-venv>` in this design. In that release, `checkov/cloudformation/checks/resource/aws/SQSQueueEncryption.py` reads only `Properties/KmsMasterKeyId`, accepting any value, and never reads `SqsManagedSseEnabled`. A probe template, synthesized outside the repository with the repository's aws-cdk-lib 2.270.0 and scanned with that Checkov, showed:

- `SQS_MANAGED` queues fail `CKV_AWS_27`.
- `KMS_MANAGED` queues and customer-managed-key queues pass.
- `KMS_MANAGED` adds no IAM statement for senders or consumers, because CDK drops grants on the imported `alias/aws/sqs`.
- A customer managed key adds `kms:Decrypt`, `kms:Encrypt`, `kms:ReEncrypt*` and `kms:GenerateDataKey*` for each `grantSendMessages` sender, and `kms:Decrypt` for each `SqsEventSource` consumer. Roles with hand-written `sqs:SendMessage` statements get nothing.

The implementation repeats the Checkov run on the real templates ([R14 SQS tests](#r14-sqs-tests)).

The decision for each pair:

| Pair | Encryption | Reason |
|---|---|---|
| `dda-portal-autolabel-queue` and its DLQ | AWS managed key | Every principal is a Lambda role in this account, or SQS redrive. The check passes with no IAM change and no key charge |
| `dda-portal-camera-shadow-reports` and its DLQ | SSE-SQS | Use-case accounts' rule roles send to the queue. The AWS managed key would stop camera-registry sync from every cross-account use case (Requirements 14.4 and 4). A customer managed key would need a UseCaseAccountStack change, deployed to every use-case account before the portal switches over, plus key charges |
| `dda-portal-account-sync-acks` and its DLQ | SSE-SQS | The queue policy deliberately admits use-case accounts, and the AWS managed key would silently close that path. SSE-SQS keeps it open and treats the two cross-account queues alike |

Each DLQ takes its source queue's encryption, so a redrive pair never mixes encryption types. Both the automatic move after 3 receives and an operator-started DLQ redrive then stay within one encryption setting.

The residual: on rescan, `CKV_AWS_27` keeps reporting the four SSE-SQS queues, although they are encrypted at rest, because the check doesn't read `SqsManagedSseEnabled`. The owner chooses one of two paths ([Owner decisions before implementation](#owner-decisions-before-implementation), "SQS rescan"):

- Recommended: close the four rescan findings under Requirement 17.6 as `FALSE_POSITIVE` with `scanner-misread`, citing `SqsManagedSseEnabled: true` and the check's source.
- Take the [customer managed key option](#r14-sqs-owner-option-customer-managed-key) for the two pairs.

SSE-SQS encrypts messages at rest with keys that SQS manages. A customer managed key would add customer control over the key and a CloudTrail record of each use, at the cost of a cross-account rollout.

### R14 SQS change

In `compute-stack.ts`, each queue gains one property. Nothing else changes: grants, queue policies, redrive policies and event sources stay as they are.

| Construct | Line | Property added |
|---|---|---|
| `CameraShadowReportDLQ` | 1672 | `encryption: sqs.QueueEncryption.SQS_MANAGED` |
| `CameraShadowReportQueue` | 1681 | `encryption: sqs.QueueEncryption.SQS_MANAGED` |
| `AccountSyncAckDLQ` | 2047 | `encryption: sqs.QueueEncryption.SQS_MANAGED` |
| `AccountSyncAckQueue` | 2056 | `encryption: sqs.QueueEncryption.SQS_MANAGED` |
| `DdaAutolabelDLQ` | 2558 | `encryption: sqs.QueueEncryption.KMS_MANAGED` |
| `DdaAutolabelQueue` | 2564 | `encryption: sqs.QueueEncryption.KMS_MANAGED` |

A comment above each pair records the reason. For the two cross-account pairs:

```ts
    // SQS-managed SSE (R14.3). Use-case accounts may send to this queue. SSE-SQS
    // needs no KMS permission from any sender, while an AWS managed key can't be
    // used across accounts (R14.4). The DLQ matches its source queue.
```

For the auto-label pair:

```ts
    // The AWS managed SQS key (R14.3). Only Lambda roles in this account and SQS
    // redrive use these queues, and that key's policy admits them through SQS
    // without a grant. A sender in another account or an AWS service sender
    // would need SSE-SQS or a customer managed key instead.
```

### R14 SQS preserving function

- `KmsMasterKeyId` and `SqsManagedSseEnabled` on AWS::SQS::Queue update with no interruption. Queue names, URLs, ARNs, queue policies, redrive policies, event source mappings and queued messages all stay (Requirement 16.2).
- SSE-SQS is probably no change at all on the live queues, since it's the default. No sender or consumer gains a KMS dependency. `DDACameraShadowRuleRole` and the rest of UseCaseAccountStack don't change.
- AWS managed key on the auto-label pair: messages already queued stay receivable after the switch, and new messages are encrypted under the key. `DdaLabelingWorker`, `DdaAutolabelWorker`'s event source mapping and SQS redrive all act in this account, and the key's policy admits them. An operator who starts a DLQ redrive from the console uses a principal in this account, which the key's policy also admits.
- No IAM statement changes, so the IAM synth gate's result doesn't move, and nothing user-facing changes.

### R14 SQS errors

| Operation | Failure | Recoverable or fatal | Caller receives | Logged |
|---|---|---|---|---|
| Stack update sets the encryption property | SQS rejects the attribute | Fatal for that deploy. CloudFormation rolls back | A failed deploy | Stack events |
| `send_message_batch` to the auto-label queue | KMS throttles or denies the whole call | Recoverable. `dda_labeling_worker.py:548` doesn't catch the `ClientError`, so it propagates out of the distributor and the asynchronous invocation is retried | The exception | ERROR with traceback, from the Lambda runtime |
| One entry of a batch fails | As today | Recoverable | The entry counts as failed | ERROR `<n> auto-label messages failed to enqueue` (existing) |
| The event source mapping receives from the auto-label queue | KMS error | Recoverable. Messages stay queued and the mapping retries | Nothing | The mapping's processing state; no function log |
| Any SSE-SQS queue | None new: SQS makes no KMS call on a sender's behalf | | | |

The AWS managed key admits principals in this account, so denials aren't expected. Throttling is unlikely too: with no `KmsDataKeyReusePeriodSeconds` set, SQS reuses each data key for 300 seconds.

### R14 SQS tests

Jest cases in `security-scan-remediation-infra.test.ts`:

- Table-driven over the six queue names: the expected encryption property is present and the other is absent. `QueueName`, `RedrivePolicy` (`maxReceiveCount` 3), `MessageRetentionPeriod` and `VisibilityTimeout` are unchanged.
- Each auto-label queue's queue policy holds only the `enforceSSL` deny, and no `AWS::IoT::TopicRule`, `AWS::Events::Rule` or `AWS::SNS::Subscription` references either auto-label queue. The failure message says that a sender in another account, or an AWS service sender, needs SSE-SQS or a customer managed key. This test enforces in the CDK layer the same-account precondition of the AWS managed key.
- No IAM statement in the stack names `alias/aws/sqs`.
- The existing queue and queue-policy tests in `camera-registry-infra.test.ts` and `camera-shadow-sync-provisioning.test.ts` pass unchanged. They pin the camera-shadow queue's redrive policy and exact two-statement queue policy, and the rule roles' grants: `DDACameraShadowRuleRole`'s `SendCameraShadowReports` and `UserAccountsShadowRuleRole`'s `SendAccountSyncAcks`, each `sqs:SendMessage` alone.

Checkov, as part of the local rescan for these resources (Requirement 17.4). Run the pinned 3.2.255 against copies, made outside the repository, of the fresh fixture synth of both stacks and of the two refreshed Baseline_Templates. For example:

```bash
CHECKOV="<triage-venv>/bin/checkov"
"$CHECKOV" -f /tmp/r14-r15-scan/EdgeCVPortalComputeStack.template.json --framework cloudformation \
  --check CKV_AWS_26,CKV_AWS_27,CKV_AWS_119,CKV_AWS_107,CKV_AWS_108,CKV_AWS_109,CKV_AWS_111,CKV_AWS_7,CKV_AWS_33 -o json
```

The expected failures depend on the input:

- Each refreshed Baseline_Template: exactly `CKV_AWS_27` on `CameraShadowReportDLQ`, `CameraShadowReportQueue`, `AccountSyncAckDLQ` and `AccountSyncAckQueue`.
- The fresh ComputeStack synth: those four, plus `CKV_AWS_26` on `CognitoAdminActivityTopic`. That topic postdates the committed Baseline_Template and has no key, and this spec doesn't encrypt it ([R14 SNS observations](#r14-sns-observations)).
- The fresh UseCaseAccountStack synth: none. It has no topic, queue, table or key, and its one flagged IAM carrier passes after R15 site 4.

Any other failure among these checks is a defect, unless the same check also fails on the same logical id in a fresh synth at `4a3f960`, made in the baseline worktree ([Baseline run at 4a3f960](#baseline-run-at-4a3f960)). Such a failure predates this spec, so it is recorded as an observation and not fixed here. Also run every check over each refreshed Baseline_Template and each fresh synth, and over their `4a3f960` counterparts: the committed template and the baseline worktree's synth. No check id may fail on a logical id where it passed at `4a3f960`.

The CDK and Checkov probes behind the probe-based claims in R14 and R15 weren't kept. These runs repeat them on the real templates, so each run's input copies and JSON output are kept in the triage workspace outside the repository, next to the triage partials.

After a deploy to a development account, the owner:

- reads `KmsMasterKeyId` and `SqsManagedSseEnabled` for the six queues with `aws sqs get-queue-attributes`;
- runs one auto-labeling job end to end;
- confirms redrive on the auto-label pair: disable `DdaAutolabelWorker`'s event source mapping, send one test message, and receive it with `--visibility-timeout 0` until SQS moves it to `dda-portal-autolabel-queue-dlq`. Then delete the test message from the DLQ and re-enable the mapping.

### R14 SQS owner option: customer managed key

If the owner wants `CKV_AWS_27` to pass on all six queues, the camera-shadow and account-sync-ack pairs move to one customer managed key:

- `new kms.Key(this, 'PortalQueuesKey', { alias: 'dda-portal/queues', enableKeyRotation: true, removalPolicy: cdk.RemovalPolicy.RETAIN })`. A key-policy statement lets each trusted use-case account (`iam.AccountPrincipal`) use `kms:GenerateDataKey` and `kms:Decrypt`, conditioned on `kms:ViaService` `sqs.<region>.amazonaws.com`.
- The four queues take `encryption: sqs.QueueEncryption.KMS` and `encryptionMasterKey: portalQueuesKey`.
- `CameraShadowRuleRole` and `UserAccountsShadowRuleRole` each get `portalQueuesKey.grant(role, 'kms:GenerateDataKey', 'kms:Decrypt')`. The handlers' `grantSendMessages` and `SqsEventSource` add their own grants.
- In UseCaseAccountStack, `DDACameraShadowRuleRole` gains `kms:GenerateDataKey` and `kms:Decrypt` on `arn:aws:kms:<region>:<portal account>:key/*`, conditioned on `ForAnyValue:StringEquals` `kms:ResourceAliases` `alias/dda-portal/queues`. The condition stands in for the key id, which that stack can't know.
- Rollout order: UseCaseAccountStack in every use-case account first, then ComputeStack. A use-case account that hasn't redeployed loses its camera shadow reports from the switch until it does. That is the lock-out Requirement 4 warns about.
- Approvals file: ComputeStack gains KMS grants for `CameraShadowRuleRole`, `UserAccountsShadowRuleRole`, `CameraSyncRole` and `AccountSyncRole`, and UseCaseAccountStack gains one statement. ComputeStack gains two resources.

Recommendation: take this option only if the owner wants these four findings to clear and accepts that rollout.

## r14-dynamodb-cmk

This section remediates `CKV_AWS_119` on the two portal-account tables (Requirement 14.5). Both are reported in `iam_baseline_EdgeCVPortalComputeStack.template.json`.

| finding_id | Reported at | Logical id | CDK source | Unfixed_Snapshot twin (`test-only`) |
|---|---|---|---|---|
| `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-5` | `:31543` | `EdgeCredentialsTable60F80243` | `compute-stack.ts:2015-2026` | `950b9d60-0315-4329-9633-02b8aa44298c-8` |
| `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-6` | `:31570` | `AccountSyncTable789D4162` | `compute-stack.ts:2031-2042` | `950b9d60-0315-4329-9633-02b8aa44298c-9` |

One new customer managed key serves both tables. Every principal that reads or writes them gets key access before either table switches to the key. The key's cost is an owner decision (Requirement 4.5, open question 2), and the recommendation is to approve the single key.

### R14 DynamoDB current behavior

- Neither table sets `encryption`, so DynamoDB encrypts them with its default AWS owned key, and the template has no `SSESpecification`. Both tables have point-in-time recovery enabled and `RemovalPolicy.RETAIN`.
- `CKV_AWS_119` passes only when `SSEEnabled` is true and `KMSMasterKeyId` is present (`DynamoDBTablesEncrypted.py` in 3.2.255). The AWS managed key, `TableEncryption.AWS_MANAGED`, renders `SSEEnabled: true` without a key id and still fails, as the triage probe confirmed. Requirement 14.5 asks for a customer managed key regardless.
- `dda-portal-edge-credentials` holds salted one-way PBKDF2 verifiers, never plaintext (`:2011-2014`).

Every principal of the two tables:

| Principal | Tables | Grant | Runtime use |
|---|---|---|---|
| `UserAdminRole` (`UserAdminHandler`) | Both | `grantReadWriteData` (`:2201`, `:2202`) | `user_admin.py:615`, `:879`, `:904`, `:1878`, `:1964`, `:2013`, `:2144` |
| `AccountSyncRole` (`AccountSyncHandler`) | `dda-portal-account-sync` | `grantReadWriteData` (`:2156`) | `account_sync.py`, for sync attempts, ack ingest and the 5-minute pass |
| `DevicesRole` (`DevicesHandler`) | `dda-portal-account-sync` | `accountSyncTable.grant(devicesHandler, 'dynamodb:DeleteItem')` (`:2161`) | `devices.py:1134-1141`, a `delete_item` with `ReturnValues='ALL_OLD'` when a device is removed |

No cross-account principal reads or writes either table. Use-case-account roles reach devices through IoT shadows, and no other stack or script names these tables.

### R14 DynamoDB change

In `compute-stack.ts`, import `aws-cdk-lib/aws-kms` and create the key before the two tables:

```ts
    // Customer managed key for the two portal-account tables (R14.5). One key
    // serves both, because the same principals use them. Rotation is on. The
    // key is retained like the tables: a retained table whose key is deleted
    // can't be read.
    const accountTablesKey = new kms.Key(this, 'AccountTablesKey', {
      alias: 'dda-portal/account-tables',
      description:
        'DDA Portal: encryption at rest for dda-portal-edge-credentials and ' +
        'dda-portal-account-sync. Disabling or deleting it makes both tables unreadable.',
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
```

Both table definitions gain two properties, and nothing else in them changes:

```ts
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: accountTablesKey,
```

After `userAdminRole`'s grants (after `:2202`), add one policy that gives all three principals key access and that both tables depend on:

```ts
    // Key access for every principal of the two tables, in a policy of its own
    // that both tables depend on. CloudFormation therefore attaches key access
    // before it switches either table to the key. grantReadWriteData adds the
    // same grant to the AccountSync and UserAdmin default policies, but those
    // policies reference the tables and update after them. DevicesRole gets key
    // access only here: Table.grant() with an explicit action list adds none.
    const accountTablesKeyAccess = new iam.Policy(this, 'AccountTablesKeyAccess', {
      roles: [accountSyncHandler.role!, userAdminRole, devicesHandler.role!],
      statements: [new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['kms:Decrypt', 'kms:DescribeKey', 'kms:Encrypt', 'kms:ReEncrypt*', 'kms:GenerateDataKey*'],
        resources: [accountTablesKey.keyArn],
      })],
    });
    edgeCredentialsTable.node.addDependency(accountTablesKeyAccess);
    accountSyncTable.node.addDependency(accountTablesKeyAccess);
```

Decisions behind this shape:

- One key, not two. The tables hold related account data and share a principal: `UserAdminRole` uses both. Two keys would double the key charge without separating access.
- Automatic rotation is on. Checkov's `CKV_AWS_7` requires it for customer managed keys, and the probe key passed `CKV_AWS_7` and `CKV_AWS_33`.
- The key keeps CDK's default key policy, which gives the account root `kms:*` and so delegates to IAM. Access then comes from role grants alone, with no principal list in the key policy to maintain.
- The dedicated policy fixes deploy ordering. CloudFormation updates each table before the default policies that reference it, so without the dedicated policy a principal could briefly be refused the key after its table switched. The dedicated policy references only the key and the roles, so the dependency creates no cycle. Roles attach their overflow managed policies through each policy's `Roles` property, not the reverse.
- The action list matches what `grantReadWriteData` emits for a customer-managed-key table, as the probe confirmed, so all three principals hold the same key access. `DevicesRole`'s `delete_item` asks for `ALL_OLD`, so DynamoDB must decrypt the item on that principal's behalf.
- Under CDK's automatic grants, `AccountSyncRole` and `UserAdminRole` hold the key grant twice. The IAM synth gate counts each grant once per principal, so the duplicate has no effect.

This adds three ComputeStack resources: the key, its alias and the policy.

### R14 DynamoDB preserving function

- `SSESpecification` on AWS::DynamoDB::Table updates with no interruption. The table stays available while DynamoDB re-encrypts its table key under the new key. Table names, items, point-in-time recovery and the Retain policies stay (Requirement 16.2).
- Deploy order: the key, then `AccountTablesKeyAccess`, then the tables, then the default policies. Every principal holds key access before either table needs it.
- Point-in-time restores of backups taken after the change need the key. Administrators have it through the default key policy. Backups taken before the change still restore under the AWS owned key.
- The admin console's user list, the `edgeCapable` flag, account sync and device removal behave as before. Nothing user-facing changes.

### R14 DynamoDB errors

| Operation | Failure | Recoverable or fatal | Caller receives | Logged |
|---|---|---|---|---|
| Key creation, or a table's switch to the key | KMS or DynamoDB rejects the request | Fatal for that deploy. CloudFormation rolls back. A key created during a failed first deploy is retained by its policy; the owner schedules its deletion | A failed deploy | Stack events |
| Table access by a principal without key access | DynamoDB returns `AccessDeniedException`, naming KMS | Recoverable by fixing the grant. The Jest invariant below stops this from shipping | `user_admin.py`: 500 `Internal server error`. `devices.py`: a warning in the removal summary, and the device is still removed. `account_sync.py`: ack ingest reports a batch item failure and SQS redelivers the ack; the scheduled pass retries 5 minutes later | The handlers' existing logging: ERROR in `user_admin.py`, warning text in `devices.py` |
| The key is disabled or scheduled for deletion | Every read and write of both tables fails. If the key stays unavailable, DynamoDB eventually marks the tables inaccessible | Recoverable by re-enabling the key in time. The key description warns against disabling it | As above | CloudTrail and the handlers' logs |

### R14 DynamoDB tests

Jest cases in `security-scan-remediation-infra.test.ts`:

- The stack has exactly one new `AWS::KMS::Key` with `EnableKeyRotation` true, `DeletionPolicy` and `UpdateReplacePolicy` `Retain`, and the alias `alias/dda-portal/account-tables`.
- Both tables have `SSESpecification` `{SSEEnabled: true, SSEType: KMS, KMSMasterKeyId: <key ARN>}`, with names, point-in-time recovery and `Retain` unchanged, and both depend on `AccountTablesKeyAccess`.
- Invariant: every principal of the tables has key access. For every role whose policies grant a `dynamodb:` action on either table or its indexes, that role also holds `kms:Decrypt` on the key, and the set of such roles is exactly `UserAdminRole`, `AccountSyncRole` and `DevicesRole`. This test owns Requirement 14.5's "every principal" rule, so a future table grant without key access fails before deploy.

The IAM synth gate needs three approved additions ([R14 and R15 fixtures and deploy constraints](#r14-and-r15-fixtures-and-deploy-constraints)). Checkov `CKV_AWS_119` must pass on both tables, and `CKV_AWS_7` and `CKV_AWS_33` on the key.

After a deploy to a development account, the owner:

- runs `aws dynamodb describe-table` for both tables and expects `SSEDescription` with `Status` `ENABLED`, `SSEType` `KMS` and the new key's ARN;
- lists users in the admin console, runs an account sync to a test device, and removes a test device, expecting no warnings.

## r15-least-privilege-iam

This section remediates the five IAM findings against Baseline_Templates (Requirement 15). The eight Unfixed_Snapshot IAM findings are `test-only`, and none of the thirteen is a `platform-constraint` (Requirement 15.3).

| Rule | finding_id | Reported at, logical id | Flagged actions | CDK source | Unfixed_Snapshot twin |
|---|---|---|---|---|---|
| `CKV_AWS_109` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-1` | ComputeStack baseline `:1717`, `StationProvisioningRoleDefaultPolicyD9DAB7DD` | `iot:AttachPolicy` | `compute-stack.ts:986-1002` | `950b9d60-0315-4329-9633-02b8aa44298c-3` |
| `CKV_AWS_111` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-2` | same policy | `iot:CreatePolicy`, `iot:CreatePolicyVersion`, `iot:DeletePolicyVersion`, `iot:CreateRoleAlias` | `compute-stack.ts:986-1002` | `950b9d60-0315-4329-9633-02b8aa44298c-5` |
| `CKV_AWS_111` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-3` | ComputeStack baseline `:3903`, `DevicesRoleOverflowPolicy23F23C37D` | `iot:CloseTunnel`, `iot:RotateTunnelAccessToken` | `compute-stack.ts:1104-1121` | `950b9d60-0315-4329-9633-02b8aa44298c-4` |
| `CKV_AWS_111` | `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-4` | ComputeStack baseline `:46006`, `EnableSageMakerEventBridgeServiceRoleDefaultPolicy6B859A55` | `events:PutRule`, `events:DeleteRule` | `compute-stack.ts:3636-3645` | `950b9d60-0315-4329-9633-02b8aa44298c-7` |
| `CKV_AWS_111` | `ec660377-3ea5-4738-962a-1e6d384643fa-0` | UseCaseAccountStack baseline `:110`, `DDASageMakerExecutionRoleDefaultPolicyAABC5DB6` | `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`, and `sagemaker:` `CreateTrainingJob`, `StopTrainingJob`, `CreateCompilationJob`, `StopCompilationJob`, `CreateModel`, `DeleteModel` | `usecase-account-stack.ts:250-262`, `:265-297` | `e559a804-79f1-4597-a014-0019733518c3-0` |

Each flagged statement is split. Actions without resource-level permissions stay on `'*'` (Requirement 15.2), and so does `iot:ListTagsForResource`, a read action that has none for tunnels. Every other action moves to the ARNs or ARN patterns it uses at runtime. Every action that each role holds today stays granted (Requirement 15.4), so no permission a deployed function, device role or job uses is removed.

### R15 current behavior

1. `StationProvisioningRole` (`compute-stack.ts:949-957`). Only the `QuickSetupHandler` role can assume it. The quick_setup Lambda assumes it with a per-device session policy and hands the resulting short-lived credentials to the station, where `setup_station.sh` and the Greengrass installer use them. The flagged statement (`:986-1002`) grants 11 IoT actions on `'*'`. Its rationale comment (`:975-985`) says the session policy narrows these calls, but `session_policy.py:104-111` grants the same actions on `'*'` too.
2. `DevicesHandler`'s secure-tunneling grant (`:1104-1121`). It gives six tunnel actions on `'*'`. The handler only calls `open_tunnel` (`devices.py:851`). For a same-account use case it calls it with the Lambda's own role, in the use case's region.
3. The SageMaker EventBridge enabler (`:3554-3645`). This custom-resource Lambda calls `put_rule` (`:3577`) and `delete_rule` (`:3616`) on the fixed rule `sagemaker-eventbridge-enabler` (`:3574`, `:3613`) on the default event bus, through `boto3.client('events')` in the stack's region. Its `Timestamp: Date.now()` property (`:3652`) makes it run on every deploy.
4. `DDASageMakerExecutionRole` (`usecase-account-stack.ts:191-195`). SageMaker assumes it for the jobs the portal starts in each use-case account (`training.py:552` and `:890`, `compilation.py:652`, `labeling.py:470`, `model_converter.py:416`). The logs statement (`:250-262`) and the SageMaker job and model statement (`:265-297`) are both on `'*'`.

Which actions lack resource-level permissions comes from the IAM definition data the pinned Checkov uses (policy_sentry data bundled with 3.2.255, queried during this design): `iot:CreateKeysAndCertificate`, `iot:AttachThingPrincipal`, `iot:DescribeEndpoint`, `iot:OpenTunnel`, `iot:ListTunnels`, `events:ListRules`, `cloudwatch:PutMetricData`, `sagemaker:ListTrainingJobs`, `sagemaker:ListCompilationJobs`, `sagemaker:ListLabelingJobs` and `sagemaker:ListModels`. Every other action in these statements takes an ARN. `iot:AttachPolicy`, for example, takes the target certificate or thing group. `iot:ListTagsForResource` takes ARNs of 22 IoT resource types, but a tunnel isn't one of them, so a `tunnel/*` scope would match nothing.

### R15 change

Region and account patterns follow each statement's neighbors. IoT statements use `arn:aws:iot:*:<account>:...`, like the thing statement at `compute-stack.ts:960-974`, because a same-account use case can live in a different region from the portal stack. SageMaker and logs statements use `arn:aws:<service>:*:${this.account}:...`, like the `DDAPortalAccessRole` statements in the same file (`usecase-account-stack.ts:388`, `:545`).

Site 1, `StationProvisioningRole`. Replace `compute-stack.ts:975-1002` with:

```ts
    // IoT certificate, policy, endpoint and role-alias actions of the Greengrass
    // installer (setup_station.sh:1050) and the thing-policy shadow repair
    // (setup_station.sh:1075-1165). setup_station.sh fixes the policy and
    // role-alias names; certificate ids are generated at provisioning time, so
    // AttachPolicy is scoped to cert/*. Region '*' as in the thing statement
    // above. The per-device session policy narrows these grants further only
    // for thing and thing-group resources.
    const iotArn = (resource: string) => `arn:aws:iot:*:${cdk.Aws.ACCOUNT_ID}:${resource}`;
    stationProvisioningRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['iot:CreateKeysAndCertificate', 'iot:AttachThingPrincipal', 'iot:DescribeEndpoint'],
      // nosec: iam-resource-wildcard. These three actions have no resource-level
      // permissions; every other provisioning action is scoped below.
      resources: ['*'],
    }));
    stationProvisioningRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['iot:GetPolicy', 'iot:CreatePolicy'],
      resources: [
        iotArn('policy/GreengrassV2IoTThingPolicy'),
        // The installer names its TES certificate policy with this prefix
        // followed by the role-alias name.
        iotArn('policy/GreengrassTESCertificatePolicy*'),
      ],
    }));
    stationProvisioningRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['iot:ListPolicyVersions', 'iot:CreatePolicyVersion', 'iot:DeletePolicyVersion'],
      resources: [iotArn('policy/GreengrassV2IoTThingPolicy')],
    }));
    stationProvisioningRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['iot:AttachPolicy'],
      resources: [iotArn('cert/*')],
    }));
    stationProvisioningRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['iot:CreateRoleAlias', 'iot:DescribeRoleAlias'],
      resources: [iotArn('rolealias/GreengrassCoreTokenExchangeRoleAlias')],
    }));
```

The policy-version actions are scoped to the thing policy alone, because only the shadow repair edits versions, and only of that policy. `session_policy.py` doesn't change. The credentials' effective permissions are the intersection of the role's policy and the session policy, so scoping the role scopes the station's credentials. The session policy also stays clear of its 2048-character packed limit.

Site 2, the tunnel grant. Replace `compute-stack.ts:1104-1121` with:

```ts
    devicesHandler.role?.addToPrincipalPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['iot:OpenTunnel', 'iot:ListTunnels', 'iot:ListTagsForResource'],
      // nosec: iam-resource-wildcard. iot:OpenTunnel and iot:ListTunnels have no
      // resource-level permissions, and the read-only iot:ListTagsForResource
      // has none for tunnels; the other tunnel actions are scoped below.
      resources: ['*'],
    }));
    devicesHandler.role?.addToPrincipalPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['iot:CloseTunnel', 'iot:DescribeTunnel', 'iot:RotateTunnelAccessToken'],
      // OpenTunnel generates the tunnel ids. Region '*' because a same-account
      // use case opens tunnels in its own region with this role.
      resources: [`arn:aws:iot:*:${cdk.Aws.ACCOUNT_ID}:tunnel/*`],
    }));
```

The comment block above the statement (`:1098-1103`) stays.

Site 3, the EventBridge enabler. Declare `const SAGEMAKER_ENABLER_RULE_NAME = 'sagemaker-eventbridge-enabler';` before the function, and interpolate it into the two `rule_name = '...'` lines of the inline code (`:3574`, `:3613`). The rendered code is byte-identical, so the function's code doesn't change. Then replace `:3636-3645` with:

```ts
    enableSageMakerEventBridge.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['events:PutRule', 'events:DeleteRule', 'events:DescribeRule'],
      resources: [
        `arn:aws:events:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:rule/${SAGEMAKER_ENABLER_RULE_NAME}`,
      ],
    }));
    enableSageMakerEventBridge.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['events:ListRules'], // no resource-level permissions
      resources: ['*'],
    }));
```

A default-bus rule's ARN has no bus segment, and the Lambda's client runs in the stack's region, so `cdk.Aws.REGION` is exact.

Site 4, `DDASageMakerExecutionRole`. Replace `usecase-account-stack.ts:249-297` with:

```ts
    // SageMaker job metrics and logs. PutMetricData has no resource-level
    // permissions. SageMaker writes training, compilation, labeling and
    // processing job logs under /aws/sagemaker/, the pattern the
    // DDAPortalAccessRole CloudWatchLogs statement also uses.
    this.groundTruthRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
      })
    );
    this.groundTruthRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents', 'logs:DescribeLogStreams'],
        resources: [`arn:aws:logs:*:${this.account}:log-group:/aws/sagemaker/*`],
      })
    );

    // SageMaker job and model actions, scoped by resource type. Job and model
    // names come from the use case and model, not from a fixed prefix (a dda-*
    // prefix broke compilation status reads; see compute-stack.ts:390-399).
    this.groundTruthRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'sagemaker:CreateTrainingJob', 'sagemaker:DescribeTrainingJob', 'sagemaker:StopTrainingJob',
          'sagemaker:CreateCompilationJob', 'sagemaker:DescribeCompilationJob', 'sagemaker:StopCompilationJob',
          'sagemaker:DescribeLabelingJob',
          'sagemaker:CreateModel', 'sagemaker:DescribeModel', 'sagemaker:DeleteModel',
        ],
        resources: [
          `arn:aws:sagemaker:*:${this.account}:training-job/*`,
          `arn:aws:sagemaker:*:${this.account}:compilation-job/*`,
          `arn:aws:sagemaker:*:${this.account}:labeling-job/*`,
          `arn:aws:sagemaker:*:${this.account}:model/*`,
        ],
      })
    );
    this.groundTruthRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'sagemaker:ListTrainingJobs', 'sagemaker:ListCompilationJobs',
          'sagemaker:ListLabelingJobs', 'sagemaker:ListModels',
        ],
        // nosec: iam-resource-wildcard. The List* actions have no resource-level
        // permissions; every job and model action is scoped by type above.
        resources: ['*'],
      })
    );
```

`// nosec` markers (Requirement 18.4). The repository's own IAM audit (`test/backend-test/security/iam_audit.py`, enforced by `test_iam_bug_condition_exploration.py::test_iam_audit_returns_no_disallowed_hits`) allows a CDK statement on `'*'` that has IoT or SageMaker actions only when the statement block carries a marker. The three markers above take the place of the markers already in the statements they split (`compute-stack.ts:1001` and `:1114-1119`, `usecase-account-stack.ts:288-294`). No statement that lacks a marker today gains one, and each marker now covers only actions without resource-level permissions. The block comment at `compute-stack.ts:975-985` loses its `nosec` wording and its claim about the session policy. Bandit, Semgrep OSS and Checkov don't read `// nosec` in TypeScript.

Probe check: the four split shapes, synthesized with aws-cdk-lib 2.270.0, pass `CKV_AWS_107`, `CKV_AWS_108`, `CKV_AWS_109` and `CKV_AWS_111` in Checkov 3.2.255. The probe had `iot:ListTagsForResource` on `tunnel/*`. On `'*'` it is a read action, which none of these four checks flags, and the Checkov run in [R15 tests](#r15-tests) checks the final shape.

### R15 preserving function

Every runtime call keeps its permission:

| Site | Action | Runtime caller | Resource at runtime | Granted on, after the change |
|---|---|---|---|---|
| 1 | `iot:CreateKeysAndCertificate`, `iot:AttachThingPrincipal`, `iot:DescribeEndpoint` | Greengrass installer | None, or not scopable | `'*'` |
| 1 | `iot:GetPolicy`, `iot:CreatePolicy` | Installer, for the thing policy and the TES certificate policy; `setup_station.sh:1098` | `policy/GreengrassV2IoTThingPolicy`, `policy/GreengrassTESCertificatePolicyGreengrassCoreTokenExchangeRoleAlias` | Those two policy patterns |
| 1 | `iot:AttachPolicy` | Installer, attaching both policies to the new certificate | `cert/<new id>` | `cert/*` |
| 1 | `iot:ListPolicyVersions`, `iot:CreatePolicyVersion`, `iot:DeletePolicyVersion` | Shadow repair (`setup_station.sh:1156-1165`) | `policy/GreengrassV2IoTThingPolicy` | That policy |
| 1 | `iot:CreateRoleAlias`, `iot:DescribeRoleAlias` | Installer | `rolealias/GreengrassCoreTokenExchangeRoleAlias` | That alias |
| 2 | `iot:OpenTunnel` | `devices.py:851` | Not scopable | `'*'` |
| 2 | The other five tunnel actions | No caller in the repository | Tunnel ARNs | `'*'` for `ListTunnels` and `ListTagsForResource`, which the IAM data can't scope to tunnels; `tunnel/*` for the rest |
| 3 | `events:PutRule`, `events:DeleteRule` | Enabler inline code (`:3577`, `:3616`) | `rule/sagemaker-eventbridge-enabler`, stack region | That rule |
| 4 | Logs and metrics | SageMaker, for each job | `/aws/sagemaker/...` log groups and streams | `log-group:/aws/sagemaker/*`, which also matches the stream ARNs under each group; `'*'` for `PutMetricData` |
| 4 | Job and model actions | SageMaker, for portal-started jobs | `training-job/*`, `compilation-job/*`, `labeling-job/*`, `model/*`, in this account | Those types |

Edge cases:

- Names fixed outside CDK. `setup_station.sh:1050` passes `--thing-policy-name GreengrassV2IoTThingPolicy` and `--tes-role-alias-name GreengrassCoreTokenExchangeRoleAlias`, and `:1075` hard-codes the thing policy. A Jest test ties those literals to the ARNs above.
- Discovered policy names. `setup_station.sh:1101-1117` falls back to a policy discovered on a pre-provisioned device's certificate. Under Quick Setup credentials that path already fails, because the role lacks `iot:ListThingPrincipals` and `iot:ListAttachedPolicies` (`:1107`, `:1110`). Operators who run setup with their own credentials don't use this role. Scoping therefore removes no working path.
- Cross-account Quick Setup uses the use-case account's `DDAPortalAccessRole`, which this change doesn't touch.
- Deploy ordering. The enabler custom resource depends on its function, and the function depends on its role's default policy, so the scoped policy is in place before the custom resource runs. UseCaseAccountStack doesn't minimize policies, so each role's statements change within one policy document. In ComputeStack, policy minimization may move a statement between a role's default policy and an overflow policy. A call made at that moment could be briefly refused, as on any deploy that changes those policies; the affected calls (Quick Setup, opening a tunnel) are user-initiated and retryable.
- UseCaseAccountStack ships when each use-case account redeploys it. Until then, that account keeps the broader policy, with no effect on function. Nothing has to deploy in a particular order.
- Nothing user-facing changes (Requirement 16.6).

### R15 errors

| Site | Failure if a scope were wrong | Recoverable or fatal | Caller receives | Logged |
|---|---|---|---|---|
| 1 | The installer gets `AccessDeniedException`, and `setup_station.sh` records `Greengrass provisioning failed` | Recoverable. Fix the scope and re-run Quick Setup; the station isn't provisioned meanwhile | The operator sees the error in the setup summary | The setup log on the station |
| 2 | Only actions no caller uses are affected | None | | |
| 3, create or update | `put_rule` is denied, the custom resource returns `FAILED`, and the stack update rolls back | Fatal for that deploy, and visible on the first one | A failed deploy | ERROR `Error in SageMaker EventBridge enabler` from the function, and stack events |
| 3, delete | `delete_rule` is denied; the existing code prints the error and still reports success | Not fatal. A rule with no targets may be left behind | | The function's log |
| 4 | A job can't write logs, or can't be described or stopped | Recoverable by fixing the scope and redeploying UseCaseAccountStack | The job's failure status in the portal | The SageMaker job's failure reason |

Each change adds no external input. Every name in the new ARNs is a constant in the code or in `setup_station.sh`.

### R15 tests

Jest cases in `security-scan-remediation-infra.test.ts` use a helper that maps each action to the set of resources a role is granted, across all of the role's policy carriers: default policy, overflow managed policies and inline policies. The helper is unaffected by ComputeStack's policy minimization.

- `StationProvisioningRole`: holds the same 11 actions as before, each on exactly the resources in the site 1 code. Only `iot:CreateKeysAndCertificate`, `iot:AttachThingPrincipal` and `iot:DescribeEndpoint` are on `'*'`. The thing statement and the TES statement are unchanged.
- Names coupling: the test reads `station_install/setup_station.sh` and asserts that it passes `--thing-policy-name GreengrassV2IoTThingPolicy` and `--tes-role-alias-name GreengrassCoreTokenExchangeRoleAlias` and sets `gg_thing_policy="GreengrassV2IoTThingPolicy"`. The CDK layer owns these ARNs, and this test keeps them in step with the installer's arguments.
- `DevicesRole`: `iot:OpenTunnel`, `iot:ListTunnels` and `iot:ListTagsForResource` are on `'*'`, and `iot:CloseTunnel`, `iot:DescribeTunnel` and `iot:RotateTunnelAccessToken` are on `arn:aws:iot:*:<account>:tunnel/*` only.
- Enabler role: `events:PutRule`, `events:DeleteRule` and `events:DescribeRule` are on the rule ARN only, and `events:ListRules` is on `'*'`. The function's inline `ZipFile` contains `rule_name = 'sagemaker-eventbridge-enabler'` twice, matching the ARN's rule name.
- `DDASageMakerExecutionRole` in UseCaseAccountStack: each action's resources are as in the site 4 code.

Python:

- The IAM preservation synth gate, on the host, with the fixture changes below.
- `test/backend-test/security/test_iam_bug_condition_exploration.py`, including `test_iam_audit_returns_no_disallowed_hits`.
- `edge-cv-portal/backend/tests/test_property_session_policy_scoping.py` passes unchanged, since the session policy doesn't change.

Checkov: `CKV_AWS_107`, `CKV_AWS_108`, `CKV_AWS_109` and `CKV_AWS_111` pass on the four carriers, in the fresh synth and in the refreshed Baseline_Templates.

Integration checks run by the owner on hardware or in a development account, because this pipeline has neither:

- Quick Setup of a new station in a same-account use case. Provisioning succeeds with no `AccessDenied` in the setup log. If possible, use a use case in a region other than the portal's.
- Opening an SSH tunnel from the portal to a same-account device.
- A ComputeStack deploy in which the enabler succeeds, shown by the `SageMakerEventBridgeStatus` output.
- In a development use-case account, a UseCaseAccountStack redeploy followed by one training job, one compilation job and one labeling job, with each job's logs appearing under `/aws/sagemaker/`.

### R15 observations

This spec doesn't fix these:

- The role and the session policy both omit `iot:GetPolicyVersion`, `iot:ListThingPrincipals` and `iot:ListAttachedPolicies`, which `setup_station.sh:1107`, `:1110` and `:1130` call. Under Quick Setup credentials, the shadow-statement check therefore ends inconclusive with a warning before it reaches the version actions.
- `session_policy.py` still grants the provisioning actions on `'*'`. This is harmless, because the effective permissions are an intersection, but its module comment overstates the narrowing.
- No code calls `iot:CloseTunnel`, `iot:DescribeTunnel`, `iot:ListTagsForResource`, `iot:RotateTunnelAccessToken`, `events:DescribeRule` or `events:ListRules`. They stay granted, scoped where they take ARNs, because Requirement 15.4 protects only permissions used at runtime, and removing them is a separate choice.
- `iot:OpenTunnel` and `iot:AttachThingPrincipal` stay on `'*'` because the IAM data the pinned Checkov uses lists no resource type for them, and `iot:ListTagsForResource` because that data lists no tunnel resource type for it. This design didn't check these against a live account.
- The ECR statement of `DDASageMakerExecutionRole` (`usecase-account-stack.ts:300-311`) keeps repository-scoped reads on `'*'` next to `ecr:GetAuthorizationToken`. The scan didn't flag it, since it grants no write.
- In single-account mode, `edge-cv-portal/deploy-account-role.sh` creates its own `DDASageMakerExecutionRole` with the AWS CLI instead of deploying UseCaseAccountStack. Its CloudWatch Logs policy keeps `cloudwatch:PutMetricData` and the logs actions on `"*"` (`:263-287`). Its SageMaker policy scopes every SageMaker action, the `List*` actions included, to `dda-*` job and model names (`:289-336`). No committed template holds that role, so the scan couldn't see it. R15 site 4 changes only the CDK variant, so the two variants of the role differ more after this spec.

### R14 and R15 fixtures and deploy constraints

The scanners read the committed Baseline_Templates, not a live synth. A fix in the CDK source alone therefore leaves all 15 Baseline_Template findings on rescan, so the fixtures must change too (Requirement 17.5). The current process doesn't allow that change:

- `test_synth_iam_statements_match_fixed_baseline` fails on any grant removed from the fixed baseline, and `iam_post_fix_approved_additions.json` can excuse only additions.
- `test_baseline_drift_confined_to_I1_I4` requires the fixed and unfixed baselines to differ only by `iam_baseline_cdk_i_changes.json`. The 2026-09-18 approval keeps that file to the I1 to I4 rewrite.
- The Unfixed_Snapshots can't be edited (Requirement 16.4). An earlier rebaseline mirrored feature growth into one, and this spec forbids that.

Proposed fixture path. The owner approves it before any fixture, approval file or gate test changes (Requirements 15.6 and 16.3):

1. A new owner-approved record, `test/backend-test/security/baselines/iam_baseline_post_fix_changes.json`. It has an `approval` block and, per stack:
   - `rescopes`: one entry per split statement, with its `finding_ids`, its `removed` and `added` statements in the canonical form `iam_statements_multiset` uses (sorted-key JSON, rendered as CDK renders the source statement without policy minimization), and its `unscopable_actions`: the actions that the IAM data can't scope to the resources the role uses them on, which alone may stay on `'*'`.
   - `refreshed_resources`: the logical ids whose non-IAM content is refreshed or added. In ComputeStack these are the topic, the six queues, the two tables, the key and the alias; UseCaseAccountStack has none.
2. Each Baseline_Template is generated from a fresh fixture synth, with the same context and hermetic environment as `_synth_template`, never typed by hand. In the committed file:
   - each `removed` statement is replaced by its `added` statements, in the same carrier;
   - the encryption properties (`KmsMasterKeyId`, `SqsManagedSseEnabled`, `SSESpecification`) are copied onto the nine refreshed resources;
   - the key and alias resources are added;
   - the file is written with its existing one-space indentation, so the diff touches only the listed logical ids.

   The dedicated key policy and the KMS grants don't go into the baseline. Like every other post-fix addition, they are approvals.
3. Changes to `test_preservation_iam_cdk_synth.py`:
   - `test_baseline_drift_confined_to_I1_I4` asserts that every recorded `added` statement is in the fixed baseline, reverses the recorded rescopes, and then makes its I1 to I4 comparison unchanged.
   - New `test_post_fix_rescopes_only_narrow`: in each rescope, the `added` statements hold exactly the actions of the `removed` ones, so no action is gained or lost (Requirement 15.4). Any `added` statement on `'*'` holds only that rescope's `unscopable_actions`, and no `removed` statement remains in the fixed baseline.
   - New `test_refreshed_resources_match_synth`, which skips like the synth test when CDK is unavailable: each refreshed resource's encryption property, and the whole key and alias resources, equal the fresh synth. One synth per stack is shared through a module-level cache.
4. `iam_post_fix_approved_additions.json` (Requirement 15.6). For EdgeCVPortalComputeStack only, it gains the account-tables key grant once for each of `AccountSyncRole`, `UserAdminRole` and `DevicesRole`, because the gate counts per principal, with its attribution and review entries. These are edits A1 to A3 in [IAM approvals-file updates](#iam-approvals-file-updates), which also lists what stays unchanged. No other default change in this part adds an IAM statement. The SNS and SQS customer-managed-key options would add the statements listed with them.
5. `iam_baseline_cdk_i_changes.json` and both Unfixed_Snapshots stay unchanged.

The rejected alternatives:

- Re-capturing each whole Baseline_Template would fold the 66 approved additions into the baseline, so `test_baseline_drift_confined_to_I1_I4` could no longer prove the I1 to I4 confinement.
- Adding the R15 changes to `iam_baseline_cdk_i_changes.json` would record them as part of the earlier authorization fix, which the 2026-09-18 approval rules out.

Verification for every R14 and R15 change:

1. In `edge-cv-portal/infrastructure`, run `npm run build`, then `npx jest`: the new file and all existing suites (Requirement 17.2).
2. On the host, run `python3 -m pytest -q -p no:cacheprovider --noconftest test/backend-test/security/preservation/test_preservation_iam_cdk_synth.py`. The gate skips inside the flask-app container (Requirement 17.3), so the run must report no skips. Also run `python3 -m pytest -q -p no:cacheprovider --noconftest test/backend-test/security/test_iam_bug_condition_exploration.py`.
3. Run the pinned Checkov as in [R14 SQS tests](#r14-sqs-tests).

Deploy constraints:

- ComputeStack template size (Requirement 16.1). The template is about 644 KB of the 1,000,000-byte limit. The default plan adds a few kilobytes: one key, one alias, one policy, nine properties and the split statements. Jest asserts that the minified ComputeStack template stays under 1,000,000 bytes and under 500 resources, and the task records the synthesized `EdgeCVPortalComputeStack.template.json` size before and after.
- ApiGatewayStack holds about 491 of its 500 resources. No change in this part adds a resource, output or parameter to any nested stack. The key and the policy live in ComputeStack's own scope. The task confirms that every `*.nested.template.json` in `cdk.out` has the same resource count before and after.
- In-place updates (Requirement 16.2). Every change is a no-interruption property update or a new resource: `KmsMasterKeyId` on the topic, the queues' encryption properties, the tables' `SSESpecification`, and IAM policy documents. No logical id, resource name or construct path changes, so nothing is replaced, and data, messages and subscriptions stay.
- Order. In the default plan, ComputeStack and UseCaseAccountStack deploy independently. Only the SQS customer-managed-key option imposes an order, with use-case accounts first.

## IGNORED_IMPAIRS_FUNCTION

No ledger entry has this Disposition. Requirement 4.1 applies only when every available remediation would break or degrade a function users rely on. For every real finding, this design found a remediation that keeps the function working, and Requirement 4.3 then requires `REMEDIATE`. Remediations that only add cost are owner decisions, not impairments (Requirement 4.5). Because no entry is `IGNORED_IMPAIRS_FUNCTION`, no entry carries the impaired function, affected users and mechanism of Requirement 4.2 or the compensating control of Requirement 4.4. The merge script rejects an entry of this kind that lacks those fields ([Ledger checks](#ledger-checks)), so a later owner decision or rescan finding that does meet Requirement 4.1 gets them recorded.

### Candidates considered

The first four rows are the cases Requirement 4 lists for evaluation. The rest are the other places where a fix could have cost users a function.

| Candidate | Remediation that would impair | Remediation used, and why the function holds | Disposition |
|---|---|---|---|
| 1. A hash that derives persisted ids: `B324` `e1583a8e-82f5-468a-829e-9cc91e0e5bb7-0` (`aravis.py`) and `93e57454-5aca-4e33-a9a0-fe6de96905c1-0` (`discovery.py`) | Any other digest or truncation changes every `camera_source_id`, which orphans registry entries and workflow bindings | The same SHA-1 and truncation, marked non-security in one helper. Golden ids pin the output ([R12](#r12-identifier-hashes)) | `REMEDIATE` |
| 2. HTTPS on a device-local HTTP endpoint: `scanner-x/plaintext-http` `daa2b724-2979-4511-bc86-2207336c655e-0` (`hmi/index.html`) and `f141d7e3-24b4-45e9-8c15-6b40025b70ca-0` (`hmi/triple.html`) | Rewriting the example origin to `https://` would point operators at the LocalServer listener on port 5000, which serves plain HTTP (`src/backend/app.py:396`) | None needed. The flagged text is an example LAN origin inside an HTML comment, so the page makes no such request (Requirement 13.2) | `FALSE_POSITIVE`, `scanner-misread` |
| 3. A Python feature a Device_Runtime lacks: `usedforsecurity`, added in 3.9, for the same two `B324` findings | A keyword-only call raises `TypeError` on Python 3.8, which Requirement 12.3 names | Try the keyword and fall back on `TypeError`; both paths give the same digest. No shipped image runs these modules on 3.8 ([R12 runtimes](#r12-runtimes)) | `REMEDIATE` |
| 4a. Encryption that locks out a publisher: `CKV_AWS_26` `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-7` and `scanner-x/sns-topic-encryption` `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-0` | The AWS managed SNS key refuses AWS service publishers | The AWS managed key, because both publishers are Lambda roles in the topic's account. The owner's read-only checks confirm that no service publishes before it ships; a customer managed key is the fallback ([R14 SNS encryption](#r14-sns-encryption)) | `REMEDIATE` |
| 4b. Encryption that locks out a cross-account sender: `CKV_AWS_27` `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-13`, `-8`, `-9` and `-10` | The AWS managed SQS key can't be used from another account, so camera-registry sync would stop for every cross-account use case | SSE-SQS, which needs no KMS permission from any sender. The Checkov result it leaves is decision 5 ([R14 SQS encryption](#r14-sqs-encryption)) | `REMEDIATE` |
| 4c. Encryption that locks out a reader: `CKV_AWS_119` `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-5` and `-6` | Switching a table to a key its principals can't use would break the admin console's user list, account sync and device removal | One customer managed key, with a key-access policy that CloudFormation attaches before either table switches. No cross-account principal uses the tables ([R14 DynamoDB CMK](#r14-dynamodb-cmk)). The key's cost is decision 6 | `REMEDIATE` |
| 4d. IAM scoping that removes a used permission: `CKV_AWS_109` `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-1`, `CKV_AWS_111` `-2`, `-3`, `-4` and `ec660377-3ea5-4738-962a-1e6d384643fa-0` | Scoping SageMaker jobs by a name prefix (a `dda-*` prefix once broke compilation status reads, `compute-stack.ts:390-399`), or moving actions without resource-level permissions off `'*'` | ARNs by resource type and fixed name. Actions without resource-level permissions stay on `'*'`, and every action each role holds today stays granted ([R15 least-privilege IAM](#r15-least-privilege-iam)) | `REMEDIATE` |
| 5. Hardening the Portal JWT authorizer: `490a14e8-75e3-48e4-af23-ae95676b238c-0` | The checks deny Cognito access tokens, tokens without `exp`, `aud` or `sub`, and every token while `ALLOWED_AUDIENCES` is empty | Applied as designed. No deployed API attaches the authorizer, so no client loses access. The deliberate changes are listed in [R6 preserving function](#r6-preserving-function) and go into the ledger with the tasks | `REMEDIATE` |
| 6. Confining the DDA permission walk: `24e13eb5-0be6-4586-abc1-9ca6aa3b9aab-0` | Refusing Folder image sources outside `/aws_dda` could break a station that uses one | The check runs only when a source is created or updated, and the device web UI always prefixes `/aws_dda/`. Stored sources aren't re-validated. Decision 10 covers any source created through the API outside the area ([R7 DDA permission walk](#r7-dda-permission-walk)) | `REMEDIATE` |
| 7. Containing custom Python handlers: `69ef51f8-d65b-4291-a4ae-d447bbd2367d-0` | Blocking user-authored handler code would remove the custom Python node, whose documented purpose is running it (Requirement 8.1) | Only the handler path is contained. The Portal compiler and the device planner both emit `python/<node id>/handler.py` inside the artifact ([R7 custom Python handler path](#r7-custom-python-handler-path)) | `REMEDIATE` |
| 8. The detector export floor interpreter: `feb1c815-fb14-44af-b148-6502a6f1fad8-0` and `-1` | Ignoring `ORT_FLOOR_PYTHON` would break a deployment that points it somewhere else | Nothing sets another value. The Dockerfile sets the default, and the Portal's job environment omits the variable ([R7 detector export floor run](#r7-detector-export-floor-run)) | `REMEDIATE` |
| 9. Payload reference fetching: `f035ce5a-9b67-4aac-9ec0-b5f597f0fdcc-0` | Default-deny for an empty `allowed_uri_prefixes` would break nodes configured without prefixes | Scheme, redirect and prefix checks on every hop. An empty list keeps its documented meaning, which decision 12 accepts as residual risk ([R9 payload reference fetch](#r9-payload-reference-fetch)) | `REMEDIATE` |
| 10. The vLLM registration script: `95141da5-9489-42a9-a8d5-e066182a8f68-0` | Requiring `https` and refusing redirects would break an `http` or redirecting Portal API URL | The documented value is the API Gateway `https` URL, which doesn't redirect ([R9 vLLM registration script](#r9-vllm-registration-script)) | `REMEDIATE` |
| 11. Legacy reference-image maps: the six [R10](#r10-deserialization) findings | Deleting the migration CLI would remove the conversion path that the postprocessor's error message sends operators to | The read is confined to component-owned roots and checked on the opened descriptor. A map elsewhere is refused with a fix hint | `REMEDIATE` |
| 12. Base images from public registries: the 15 `scanner-x/docker-image-source` findings | Pointing the Dockerfiles at company-internal registries would break builds for every public user | Closed under owner rule 1, the rule written for this case (Requirement 2.1). The six JetPack Dockerfiles in `src/backend` and `src/edgemlsdk` already take `BASE_REGISTRY` (default `nvcr.io`), so a deployer can build them from a mirror they control without changing the repository. Five Dockerfiles don't: four in `edge-cv-portal/plugin-build-images/`, and `src/backend/Dockerfile.x86_64_nvidia` ([Cross-cutting observations](#cross-cutting-observations), item 4) | `FALSE_POSITIVE`, `open-source-constraint` |

Three remediations only add cost, so they are owner decisions rather than impairments (Requirement 4.5): the DynamoDB key (decision 6), a customer managed SNS key if a service publisher turns up (decision 2), and the customer managed SQS key option (decision 5).

## False-positive handling

Recommendation: neither inline suppressions nor scanner-configuration excludes. The 411 `FALSE_POSITIVE` entries close through their ledger reasons. On every rescan, the matcher pairs each result with its entry by rule, path and content ([Matching rescan results to the ledger](#matching-rescan-results-to-the-ledger)), so a recurring false positive costs nothing to re-close while a new one still gets triaged. Nothing is added under Requirement 18.4 until the owner decides (decision 1).

### What the scanning platform honors

The evidence comes from the export and from local runs at `4a3f960`:

- Semgrep inline markers aren't honored. Four exported Semgrep findings sit on lines that already carry a `# nosem` marker naming the rule:
  - `python.jwt.security.unverified-jwt-decode` `490a14e8-75e3-48e4-af23-ae95676b238c-0`, with the full rule id on the same line.
  - Both `python.lang.security.deserialization.avoid-dill` findings, `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-2` and `-5`, with a short id.
  - `python.lang.security.audit.dangerous-subprocess-use-audit` `341312f4-30de-4141-9320-e02d498c1c78-0`, with the full rule id on the line and on the line above.

  Semgrep treats `# nosem` as the short form of `# nosemgrep`. The platform reported all four, so either it disables inline markers or its rule ids differ from the ones written. Either way, a new Semgrep marker can't be relied on to close anything.
- Bandit `# nosec` markers appear to be honored. The pinned Bandit 1.8.6 was run over the 25 tracked Python files that contain `# nosec`, limited to the 12 exported test ids. It reports 25 results with `--ignore-nosec` and 12 without. All 12 unmarked results are in the export, and none of the 13 marked ones is. The marked ones include the intentional LAN binds at `src/backend/app.py:387-396` and the loopback health probe at `src/backend/healthcheck.py:61`. This is an inference from one export, not documented platform behavior.
- Checkov skips: no committed template carries Checkov skip metadata, so the export can't show whether the platform honors it. CDK could emit it through `cfnOptions.metadata`. It would then also have to appear in the refreshed Baseline_Template, because the scanners read the committed fixture.
- Scanner-X: its rule set and suppression syntax aren't public.
- Configuration excludes: the repository has no scanner configuration at `4a3f960`. There is no `.bandit`, `.semgrepignore` or Checkov configuration file, and `pyproject.toml` and `setup.cfg` have no scanner section. Nothing in the export shows whether the platform would read one.

### Options and recommendation

| Approach | Would it close findings on the platform? | Cost and risk |
|---|---|---|
| Inline markers on the 411 lines | Probably for Bandit's 310. Not for Semgrep's 68. Unknown for Checkov's 15 and Scanner-X's 18 | A marker on every flagged line, each approved under Requirement 18.4: 302 in 133 test files, and 109 in shipped code, support files, vendored packages and Dockerfiles. Markers in vendored packages diverge from upstream. A marker keeps hiding its line after the code changes into a real issue |
| Configuration excludes for test code | Unknown, since nothing shows the platform reads repository configuration | A directory exclude for `test/` would also hide Shipped_Code: `test/on-hardware/register_vllm_models.py` ([R9](#r9-url-fetching)) and the two Baseline_Templates ([R14](#r14-sns-encryption), [R15](#r15-least-privilege-iam)). File-name patterns don't fit the dispositions: 22 `test-only` findings are in files without a test name, and 43 findings in test-named files aren't `test-only`. Either form would hide future credential-format values in tests, which Requirement 3.6 needs to see |
| Neither (recommended) | No. The 411 reappear on every scan | No code change. The ledger is the record, and the matcher re-closes recurring results mechanically |

### Breakdown by Sub_Reason

Computed by a script that loads `ledger.json` and filters on `disposition` and `sub_reason`:

| Sub_Reason | Entries | Where | Handling |
|---|---|---|---|
| `test-only` | 302 | 133 files in 24 directories: 263 under `test/backend-test/` (16 of them in the two Unfixed_Snapshots), 25 under `edge-cv-portal/backend/tests/`, 7 under `test/on-hardware/harness/selftest/`, 4 under `edge-cv-portal/test-sandbox/tests/integration/` and 3 under `edge-cv-portal/backend/layers/workflow_core/tests/` | Neither. New test code for this spec avoids what the credential rules match ([Testing strategy](#testing-strategy)), so it adds no `test-only` results |
| `scanner-misread` | 79 | 22 directories, in shipped and test code | Neither, and no renames ([below](#scanner-misread-findings)) |
| `open-source-constraint` | 15 | 10 Dockerfiles | Neither. They pair with their entries by rule, file and `FROM` line, including the lines `src/backend/Dockerfile.jp6` reports more than once |
| `vendored-or-untracked` | 10 | jsonschema 4.26.0, typing_extensions 4.16.0 and attrs 26.1.0, vendored under `edge-cv-portal/backend/layers/workflow_core/python/` | Neither by default. If the owner approves excludes at all, these package directories are the one place where a path exclude is exact, because they hold no first-party code. Dependency scanning covers vendored packages better than these rules do |
| `already-mitigated` | 5 | Four `B310` fetches and one subprocess call, all on the Portal side | Neither. Each control is pinned by a test ([below](#already-mitigated-findings)) |
| `platform-constraint` | 0 | | |
| Total | 411 | | |

### Already-mitigated findings

These five are real code that an existing control makes safe, so each entry stays true only while its control does. A suppression would keep hiding the line after the control regressed. Instead, a test pins each control. The matcher also flags any of these entries whose file changed since `4a3f960`, so its control gets re-read.

| finding_id | Rule and location | Control | Pinned by |
|---|---|---|---|
| `3c84a2d7-61a1-44c9-a60e-98c7586c4056-2` | `B310`, `edge-cv-portal/backend/sam-worker/handler.py:170` | An `https`-only check before any fetch (`:168-169`). The URL is presigned server-side, and only the auto-label worker may invoke this function | `edge-cv-portal/backend/tests/test_dda_sam_worker_mask_utils.py:220-225` |
| `3c84a2d7-61a1-44c9-a60e-98c7586c4056-0` | `B310`, `edge-cv-portal/backend/grounded-sam-worker/handler.py:265` | An `https`-only check in `_resolve_image_source` (`:253-254`). The URL is presigned server-side, and two functions may invoke this one | `edge-cv-portal/backend/tests/test_dda_grounded_sam_worker_utils.py:501-506` |
| `190d6250-c5f8-43fd-a531-9e286f505edc-0` | `B310`, `edge-cv-portal/backend/functions/build_source.py:1000` | The fixed host `GITHUB_API_HOST`. The owner and repository are validated against `_OWNER_RE` and `_REPO_RE` before any call | `test/backend-test/portal_builds/test_branch_discovery_property.py:243-251` and `:416` |
| `11d862e0-87f7-4b9d-9caf-70501bddd384-0` | `B310`, `edge-cv-portal/backend/functions/vllm_fit_check.py:916` | The fixed `https://huggingface.co` template `HF_MODEL_API_URL` (`:299`), with the model id URL-quoted into its path (`:947`) | Nothing at `4a3f960`, as the entry notes. New `edge-cv-portal/backend/tests/test_vllm_fit_check_url.py` |
| `a9f7d400-cc54-4012-b065-4424eedce396-0` | `python.lang.security.audit.dangerous-subprocess-use-audit`, `datasets/detection_training/_common.py:62` | A list argv without a shell. The only external value, `IMGSZ`, is an option-argument that the Portal range-checks and the entry point casts to `int` | `edge-cv-portal/backend/tests/test_detection_training_shared.py:148` |

The new test calls `_estimate_from_hf(model_id, {}, hf_fetch)` with a recording `hf_fetch` that returns `None`. Hypothesis generates model ids that include `?`, `#`, `@`, `:`, `..` and spaces. For each, `urlsplit` of the recorded URL must give scheme `https`, host `huggingface.co`, a raw path that starts with `/api/models/` and the query `blobs=true`. The prefix is checked on the path as recorded, not after normalization: `quote(hf_model_id, safe='/')` keeps `/`, so an id containing `..` yields a path that would normalize outside `/api/models/`, while the host stays fixed. This completes the Requirement 9.5 coverage for this fetch. It is Test_Code only, and the module doesn't change.

### Scanner-misread findings

For these 79, the code is what it should be, and the rule matched a pattern without the property it looks for:

| Rules | Entries | What the flagged code is |
|---|---|---|
| `B105`, `B106`, `B107` | 63 (31 in test-named files) | Names and values that contain `password`, `token` or `secret` but hold no credential: settings-table keys, failure-category and enum labels, an error code, a path placeholder, template-delimiter and empty-string comparisons, a regular-expression fragment that lists secret-like key names, the name or path of a secret rather than its value, a password-policy symbol class, and package names that tests match |
| `B104` | 4 | All-zeros address strings in fake-camera test fixtures. Nothing binds to them |
| `python.lang.security.audit.dangerous-subprocess-use-audit`, `B604` | 6 | Five calls whose non-literal arguments no outside party controls, such as the running interpreter, a bundled module path or a module constant. One `shell=` keyword passed to `dict()` |
| `B608`, `python.sqlalchemy.security.sqlalchemy-execute-raw-query` | 3 | A bash hook built in an f-string, and two calls to `WorkflowExecutor.execute` |
| `B403` | 1 | An import of `pickletools`, the opcode disassembler, matched by its prefix |
| `scanner-x/plaintext-http` | 2 | An example LAN origin inside HTML comments |
| Total | 79 | |

Renaming identifiers to dodge the name heuristics would churn Shipped_Code for no security gain, and several flagged names are settings keys or labels that other code reads. The only cheap textual change is in the two HTML comments: writing the example origin without the `http://` scheme would stop `scanner-x/plaintext-http` without changing any request. It's left out by default, because it edits shipped pages only to satisfy a scanner.

If the owner wants fewer platform results despite the recommendation, the export suggests one marker would work: Bandit `# nosec <test id>` carrying the ledger reason. The natural candidates are the 33 Bandit `scanner-misread` lines outside test-named files. Semgrep markers wouldn't help.

## Deployment and rollout

This section covers the CloudFormation changes of R14 and R15 and the R6 wiring: their keys, update behavior, order and rollback, and the fixture and approval files that move with them. Device-side and image changes are summarized at the end. Every change is an in-place update or a new resource, and nothing is replaced (Requirement 16.2). Template size and resource counts are covered in [R14 and R15 fixtures and deploy constraints](#r14-and-r15-fixtures-and-deploy-constraints) (Requirement 16.1).

### Keys and key policies

| Resources | Key | Key policy | Who can use it, and how |
|---|---|---|---|
| Topic `dda-portal-training-alerts` | AWS managed `alias/aws/sns` | Owned by AWS. It admits principals in this account through SNS only, and it can't be edited to admit a service principal or another account | Both publisher roles, with no KMS grant |
| Queue `dda-portal-autolabel-queue` and its DLQ | AWS managed `alias/aws/sqs` | The same pattern, through SQS | `DdaLabelingWorker`, `DdaAutolabelWorker`'s event source mapping and SQS redrive, all in this account, with no grant |
| Camera-shadow and account-sync-ack queue pairs | None: SSE-SQS | No KMS key | Every sender, including the use-case accounts' IoT rule roles, with no change |
| Tables `dda-portal-edge-credentials` and `dda-portal-account-sync` | New customer managed key `alias/dda-portal/account-tables`, automatic rotation, `RemovalPolicy.RETAIN` | CDK's default key policy: the account root holds `kms:*`, which delegates access to IAM. There is no principal list to maintain | `AccountTablesKeyAccess` grants `AccountSyncRole`, `UserAdminRole` and `DevicesRole`. `grantReadWriteData` adds the same KMS statement for the first two |
| Option: the topic, if a service publishes | `alias/dda-portal/training-alerts` | The default policy, plus a statement giving the service principal `kms:Decrypt` and `kms:GenerateDataKey*` under `aws:SourceAccount` | `grantPublish` adds KMS statements for both publisher roles |
| Option: the two cross-account queue pairs | `alias/dda-portal/queues` | The default policy, plus a statement for each trusted use-case account (`iam.AccountPrincipal`) with `kms:GenerateDataKey` and `kms:Decrypt` under `kms:ViaService` `sqs.<region>.amazonaws.com` | Grants for the two portal rule roles and the two handlers; `DDACameraShadowRuleRole` needs its own grant in UseCaseAccountStack |

When a table switches to the customer managed key, DynamoDB creates KMS grants on behalf of the principal that updates the table, which is CloudFormation's execution role. CDK's default bootstrap gives that role `AdministratorAccess`. In an account bootstrapped with a narrower execution policy, that role needs `kms:CreateGrant` and `kms:DescribeKey` on the key, so the owner checks it before Deploy B. Nobody may disable the key or schedule its deletion while either table uses it. The key's description says so.

### In-place updates

| Change | CloudFormation update | Kept |
|---|---|---|
| `KmsMasterKeyId` on the topic | No interruption | Topic name, ARN, export and subscriptions |
| `KmsMasterKeyId` or `SqsManagedSseEnabled` on the six queues | No interruption | Queue names, URLs, queue policies, redrive policies, event source mappings and queued messages |
| `SSESpecification` on the two tables | No interruption. Each table stays available while DynamoDB re-encrypts its table key | Table names, items, point-in-time recovery and `Retain` |
| The key, its alias and `AccountTablesKeyAccess` | New resources | |
| IAM policy documents: the R15 rescopes and the KMS grants | No interruption | Role names and ARNs |
| `ALLOWED_AUDIENCES` and the shared `edge-cv-portal/backend/functions` asset (R6) | No interruption. Every function built from that asset gets a code update with no behavior change | Function names and ARNs |
| `DDASageMakerExecutionRole`'s policy in UseCaseAccountStack | No interruption | The role |

`deploy-infrastructure.sh` runs `npx cdk deploy --all --require-approval never --force`, and `deploy-frontend.sh` runs `npx cdk deploy EdgeCVPortalComputeStack --require-approval never`, so nothing prompts on IAM or security changes. The owner's review of `npx cdk diff EdgeCVPortalComputeStack` is therefore the human gate before each deploy. It runs with the context arguments the deploy script passes, and it confirms four things:

- No resource is replaced or destroyed.
- The only new resources are the step's own: none for Deploy A, and the key, its alias and `AccountTablesKeyAccess` for Deploy B.
- The IAM changes are exactly that step's rescopes and KMS grants.
- Lambda changes are to code and environment only.

### Deploy order

In each portal account, starting with a development account:

1. Read-only checks before any deploy:
   - The three SNS publisher checks in [R14 SNS change](#r14-sns-change).
   - `aws sqs get-queue-attributes --attribute-names All` for the six queues, to record their current encryption.
   - The CloudFormation execution role's KMS permissions.
   - The synthesized template size and the nested-stack resource counts.
2. Deploy A: ComputeStack with R6, R14 SNS, R14 SQS and R15 sites 1 to 3. It adds no resources. Afterwards, run the checks in [R14 SQS tests](#r14-sqs-tests) and [R15 tests](#r15-tests): the queue attributes, one auto-labeling job, the redrive check, Quick Setup of a station, an SSH tunnel and the `SageMakerEventBridgeStatus` output.
3. Deploy B: ComputeStack with R14 DynamoDB. CloudFormation creates the key, then `AccountTablesKeyAccess`, then switches both tables, then updates the default policies. Afterwards, run `aws dynamodb describe-table` on both tables, list users in the admin console, run an account sync and remove a test device.
4. Use-case accounts: UseCaseAccountStack (R15 site 4), deployed through `deploy-account-role.sh` in each use-case account on its own schedule. It has no ordering constraint against the portal. Afterwards, run one training job, one compilation job and one labeling job, and check that their logs appear under `/aws/sagemaker/`.

Deploys A and B come from separate commits, so each account can take them one at a time. The split keeps the only change with lasting side effects, a retained key and backups taken under it, out of the IAM change's rollback path. One combined deploy would also be correct, since the dependency on `AccountTablesKeyAccess` orders key access before the table switch. `deploy-frontend.sh` and `deploy-portal.sh` redeploy ComputeStack too, so each step is deployed from the commit meant for it.

Two more constraints:

- No deploy runs during a station's Quick Setup. Site 1 changes the role that issues setup credentials, and a call can be refused briefly while policy documents swap. Quick Setup can be retried.
- The SQS customer-managed-key option, if chosen, removes step 4's independence: UseCaseAccountStack must deploy in every use-case account first ([R14 SQS owner option](#r14-sqs-owner-option-customer-managed-key)).

### Rollback

CloudFormation rolls back a failed update on its own. A manual rollback reverts the commit and redeploys. Each commit carries its code, fixtures, post-fix record and approvals together, and the revert restores all of them. Reverting only the code fails the synth gate: the Baseline_Template would hold statements the tree no longer synthesizes, and the approvals file would hold stale ones.

| Change | Rollback | During the rollback | Afterwards |
|---|---|---|---|
| Topic key | Revert and redeploy | No interruption. SNS keeps no messages at rest except while retrying delivery | The topic is unencrypted, as before |
| Auto-label pair on `alias/aws/sqs` | Revert and redeploy | Queued messages stay receivable, because the AWS managed key is never deleted | Read the queue attributes |
| SSE-SQS pairs | Revert and redeploy | Senders don't notice | Read `SqsManagedSseEnabled`. This design didn't verify what CloudFormation sets when the property leaves the template |
| Table key | Revert and redeploy. Both tables switch back to the AWS owned key in place. `AccountTablesKeyAccess` leaves the template, and CloudFormation deletes it during cleanup, after the switch | Items stay, and every principal keeps key access throughout | The key is retained and keeps billing. It stays enabled until no backup or recovery point from the key period remains: the point-in-time recovery window (up to 35 days) and any on-demand backup taken in that period. Then the owner may schedule deletion, which has a 7 to 30 day waiting period |
| Failed first Deploy B | Automatic | The tables stay on the AWS owned key | The new key is retained by its policy. The owner confirms no table uses it, then schedules its deletion |
| R15 rescopes | Revert and redeploy | A call can be refused briefly while policy documents swap | The wildcard statements are back |
| Site 3, the EventBridge enabler | Automatic. A wrong scope fails the custom resource on the same deploy, and CloudFormation rolls back | | |
| UseCaseAccountStack | Redeploy the previous version in that account | | |

### IAM approvals-file updates

The existing process, as `test_synth_iam_statements_match_fixed_baseline` and `iam_post_fix_approved_additions.json` define it:

- The gate compares grant atoms: one per effect, action, resource and condition, counted once per principal however many policies carry it. The fresh synth must equal the Baseline_Template plus the approved additions. Anything else fails: an unreviewed addition, a removal (which no approval can excuse) and a stale approval.
- An addition is approved by adding its canonical statement (sorted-key JSON) to `stacks.<stack>.approved_additions`, once per principal that holds it, with an entry under `approval.attribution`. Each addition after the original approval also gets an entry under `approval.additions_after_2026_09_18` with `spec`, `added_on`, `statements` and `reviewed`. The five existing entries follow this shape, and `approval.approved_on` stayed `2026-09-18` through all of them.

The planned edits (Requirement 15.6). Each is made only after the owner approves it:

| # | Applies when | Key in the file | Edit |
|---|---|---|---|
| A1 | Default plan, R14 DynamoDB | `stacks.EdgeCVPortalComputeStack.approved_additions` | Append three copies of the statement granting `kms:Decrypt`, `kms:DescribeKey`, `kms:Encrypt`, `kms:ReEncrypt*` and `kms:GenerateDataKey*` on `{"Fn::GetAtt": ["AccountTablesKey<hash>", "Arn"]}`, one each for `AccountSyncRole`, `UserAdminRole` and `DevicesRole`. The string is copied in canonical form from `AccountTablesKeyAccess` in the fresh fixture synth, never typed |
| A2 | Default plan | `approval.attribution` | One new key, `kms on AccountTablesKey (3 statements)`, citing Requirement 14.5 and `CKV_AWS_119` `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-5` and `-6` |
| A3 | Default plan | `approval.additions_after_2026_09_18` | One new entry. `spec` names this spec and its task, `added_on` is the approval date, `statements` is 3, and `reviewed` says the change is purely additive: one key ARN, no wildcard resource |
| A4 | Default plan | `approval.approved_on`, `approved_by`, `reviewed` and `follow_up_not_blocking`; the 66 existing statements; `stacks.DDAPortalUseCaseAccountStack` | Unchanged. The file records later additions under A3's key, as it does for the five existing ones |
| O1 | Only if the topic takes a customer managed key (decision 2) | ComputeStack | Two statements, `kms:Decrypt` and `kms:GenerateDataKey*` on the topic key, for the `TrainingEventsHandler` and `CompilationEventsHandler` roles, recorded as in A2 and A3 |
| O2 | Only with the SQS customer-managed-key option (decision 5) | ComputeStack and UseCaseAccountStack | KMS statements on the queue key for `CameraShadowRuleRole`, `UserAccountsShadowRuleRole`, `CameraSyncRole` and `AccountSyncRole`, and one for `DDACameraShadowRuleRole`, recorded for each stack |
| O3 | Only if the authorizer is deleted (decision 4) | None | The deleted role's statements are removals, so they go through the post-fix record, not this file |

The R15 rescopes don't touch this file. They replace statements in the Baseline_Template through the post-fix record.

The approval itself runs in four steps:

1. The task prepares, uncommitted: the CDK change, the regenerated Baseline_Templates, `iam_baseline_post_fix_changes.json` with an empty `approval` block, and edits A1 to A3.
2. The owner reviews three diffs: the approvals file, the post-fix record, and the Baseline_Template diff, which may touch only the logical ids the record lists.
3. Once the owner approves, the task fills in the record's `approval` block (`approved_by`, `approved_on`) and A3's `added_on`.
4. One commit carries all of it, after the gates pass on the host with no skips.

### How the synth gate, Baseline_Templates and Unfixed_Snapshots interact

| Artifact | What it holds | Read by | In this spec |
|---|---|---|---|
| Unfixed_Snapshot, one per stack | The stack's template before the earlier I1 to I4 IAM fix | `test_baseline_drift_confined_to_I1_I4`, and the scanners | Never edited (Requirement 16.4). Its sha256 is pinned in the post-fix record |
| Baseline_Template, one per stack | The template after I1 to I4: the deployed shape that the scanners read | Both gates, and the scanners | Regenerated through the post-fix record, for the R15 rescopes and the R14 refreshed resources only |
| `iam_baseline_cdk_i_changes.json` | The recorded I1 to I4 diff | `test_baseline_drift_confined_to_I1_I4` | Unchanged |
| `iam_post_fix_approved_additions.json` | Statements synthesized on top of the Baseline_Template | `test_synth_iam_statements_match_fixed_baseline` | A1 to A3 |
| `iam_baseline_post_fix_changes.json`, new | The owner-approved rescopes, refreshed resources and Unfixed_Snapshot hashes | The gates' new steps and the new tests | Created |
| `iam_out_of_scope_baseline.json` | sha256 values of out-of-scope files, including `jwt_authorizer.py` | `test_preservation_iam_out_of_scope_guard.py` | The `jwt_authorizer.py` hash is re-recorded after R6, with owner approval ([R6 tests](#r6-tests)) |

After the change, they fit together like this:

- The synth gate runs on the host and skips inside the flask-app container. It passes when the fresh atoms equal the refreshed Baseline_Template's atoms plus A1 to A3. Without the refresh, the R15 rescopes would appear as removals, which no approval can excuse. After the refresh a removal still fails, so any later narrowing goes through the post-fix record and owner approval again.
- The drift test always runs. The Unfixed_Snapshot and the refreshed Baseline_Template now differ by I1 to I4 plus the recorded rescopes. The test reverses the rescopes first and then makes its I1 to I4 comparison unchanged. It keeps proving that confinement, and the Unfixed_Snapshots stay untouched.
- Both gates read only IAM statements, so they don't see the R14 encryption properties, the key or the alias. Those are in the refreshed Baseline_Template only so the scanners read the deployed shape. `test_refreshed_resources_match_synth` keeps them equal to the synth.
- The order within a task: CDK change, `npm run build` and Jest, a fresh fixture synth in the hermetic environment `_synth_template` uses, Baseline_Templates regenerated from the record, the drafts, owner approval, the gates on the host with no skips, then the commit.

Requirement 16.3 asks for the project's existing process, and that process can't record a removal. The post-fix record extends it, so the extension is decision 8, with the wording change in [PRC-2 Fixture process extension](#prc-2-fixture-process-extension).

### Device and image rollout

- LocalServer: the R7 bridge and permission-walk changes, R9's `payload_fetch.py`, R10's `src/backend` copy and R12. These ship with the next LocalServer component build for each target, one variant at a time as the build steering requires, and are committed only after verification on a device ([Device and account verification](#device-and-account-verification)). Rollback deploys the previous component version. Camera ids don't change, so nothing on the Portal side moves.
- Detector export image (R7 `export_checkpoint.py`): rebuilt with `build-and-push.sh --push`, and the new digest is deployed through the `detectorExportImage` context or the SSM parameter. Until then, jobs run the old image unchanged. Rollback deploys the previous digest.
- Test-sandbox copy (R10): only the fixed `reference_image_map_migration.py` is copied into the sandbox staging tree, and it ships when the sandbox image is next built. The README's copy commands aren't run, because the rest of that tree lags `src/backend` ([R10 observations](#r10-observations)).
- `register_vllm_models.py` (R9): nothing to deploy. Operators get it from the repository.
- Portal Lambda code (R6): ships with Deploy A.

## Correctness Properties

These properties collect the invariants that the area sections test. Properties 1 to 4 and 7 are Hypothesis-backed. Properties 5, 6 and 8 to 11 are checked by example-based tests, invariant tests over the synthesized templates, or the rescan matcher.

### Property 1: The authorizer allows only fully verified tokens

For any token, the Portal authorizer returns Allow only if the token's RS256 signature verifies under a key from a trusted issuer's JWKS, and `exp`, `iss`, `aud`, `sub` and, for the Cognito issuer, `token_use` pass against configuration. Any change to a valid token's payload claims, re-encoded under the original header and signature, is denied.

Validates: Requirements 6.1, 6.2, 6.3 and 6.4. Checked by `edge-cv-portal/backend/tests/test_jwt_authorizer_verification.py` ([R6 tests](#r6-tests)).

### Property 2: Custom Python handlers stay inside the artifact

For any relative handler path, `build_bridges` and `build_producer_bridge` either raise `CustomPythonNodeError` or return a path whose real path lies strictly inside the component artifact directory.

Validates: Requirement 7.2. Checked by `test/backend-test/workflow_engine/test_python_bridge_handler_containment.py` ([R7 custom Python handler path](#r7-custom-python-handler-path)).

### Property 3: The DDA permission walk stays inside its area

For any string, `confine_dda_path` either raises or returns a path strictly below the DDA root and outside `/aws_dda/greengrass` and `/aws_dda/system`. `create_dda_user_directory` never changes ownership or mode outside that set.

Validates: Requirement 7.2. Checked by `test/backend-test/utils/test_dda_user_management_utils.py` ([R7 DDA permission walk](#r7-dda-permission-walk)).

### Property 4: Payload reference errors never echo URL secrets

For any URL with userinfo or query values, neither value appears in a `PayloadReferenceError` message or in `describe_reference_source` output.

Validates: Requirement 9.4. Checked by `test/backend-test/workflow_engine/test_payload_fetch.py` ([R9 payload reference fetch](#r9-payload-reference-fetch)).

### Property 5: Every fetch hop stays on allowed schemes and destinations

A payload-reference fetch requests only `http` or `https` URLs that pass the node's `allowed_uri_prefixes`, on the first hop and every redirect. It never follows a redirect from `https` to `http` or to a URL with userinfo.

Validates: Requirements 9.1 and 9.3. Checked by the redirect cases in `test_payload_fetch.py`.

### Property 6: Legacy maps are loaded only from trusted files

`dill.load` runs only on a descriptor whose `fstat` shows a regular file under a trusted root, owned by root or the effective uid, and writable by neither group nor others.

Validates: Requirements 10.1 and 10.2. Checked by `test/backend-test/lyra/test_reference_image_map_migration_confinement.py` ([R10 tests](#r10-tests)).

### Property 7: Camera ids don't change

For any text, `id_digest_hex` equals the SHA-1 hex digest of its UTF-8 bytes, on the `usedforsecurity` path and on the fallback path.

Validates: Requirements 12.2 and 12.3. Checked by `test/backend-test/camera_discovery/test_stable_id_goldens.py` ([R12 tests](#r12-tests)).

### Property 8: Every principal of the account tables can use their key

Every role that holds a `dynamodb:` action on either account table or its indexes also holds `kms:Decrypt` on the account-tables key. The set of such roles is exactly `UserAdminRole`, `AccountSyncRole` and `DevicesRole`.

Validates: Requirement 14.5. Checked by `edge-cv-portal/infrastructure/test/security-scan-remediation-infra.test.ts` ([R14 DynamoDB tests](#r14-dynamodb-tests)).

### Property 9: Rescopes only narrow

Each recorded R15 rescope grants exactly the actions of the statement it replaces, and its statements on `'*'` hold only its `unscopable_actions`: actions that the IAM data can't scope to the resources the role uses them on.

Validates: Requirements 15.1, 15.2 and 15.4. Checked by `test_post_fix_rescopes_only_narrow` and the Jest role helper ([R15 tests](#r15-tests)).

### Property 10: Synthesized grants equal the reviewed grants

For each stack, the grant atoms of a fresh fixture synth equal its Baseline_Template's atoms plus its approved additions.

Validates: Requirements 15.4 and 16.3. Checked by `test_synth_iam_statements_match_fixed_baseline`, on the host.

### Property 11: Every rescan result has a recorded disposition

Every finding in a rescan pairs, by rule, path and content, with a `FALSE_POSITIVE` or `IGNORED_IMPAIRS_FUNCTION` entry or a Rescan_Record entry, and none pairs with a `REMEDIATE` entry outside the expected list.

Validates: Requirements 17.5 and 17.6. Checked by the rescan matcher ([Matching rescan results to the ledger](#matching-rescan-results-to-the-ledger)).

## Testing strategy

Verification has three layers. Affected suites are compared with a baseline run at `4a3f960` (Requirements 17.1 to 17.3). Devices and accounts get checks this pipeline can't run. A rescan with the pinned scanners is matched against the ledger (Requirements 17.4 to 17.6). The area sections list each test; this section says where they run and how results are judged.

### Baseline run at 4a3f960

The baseline is captured once, before the first remediation task, and every task compares against it.

1. Create the baseline tree outside the working tree with `git worktree add --detach /tmp/dda-baseline-4a3f960 4a3f960`. It is removed with `git worktree remove` at closure.
2. Run both trees in the same environments, so that differences come from code:
   - Portal lane, on the host: one virtualenv built from the `remediation` branch's `edge-cv-portal/backend/requirements-dev.txt`, which adds PyJWT 2.15.0, used for both trees.
   - Container lane: the flask-app image, pinned by image id (`docker image inspect flask-app:latest --format '{{.Id}}'`) and reused for both trees. The tree is mounted at `/repo` as in the build steering's command, and results go to a mounted directory outside both trees.
   - Infrastructure lane, on the host: `npm ci`, `npm run build` and `npx jest --json --outputFile=<out>` in each tree's `edge-cv-portal/infrastructure` (Requirement 17.2).
   - Gate lane, on the host: `python3 -m pytest -q -rs -p no:cacheprovider --noconftest` for the IAM synth gate and the guard suite. Each tree has its own infrastructure `node_modules`, so the gate runs instead of skipping (Requirement 17.3).
3. Every pytest run adds `-rs --junitxml=<out>/<tree>-<area>.xml`. The results stay in the triage workspace outside the repository.

A comparison script, also outside the repository, matches results by test id: the junit `classname` and `name`, or Jest's `fullName`. For each area it reports:

| Outcome | Meaning | Effect on the task |
|---|---|---|
| New failure | Fails or errors at the head; passed or skipped at the baseline | Blocks |
| New skip | Skipped at the head; passed at the baseline | Blocks for the tests that must not skip: the IAM synth gate, the PyJWT-backed JWT tests and the `dill` round trip in the container lane |
| New test | Present only at the head | Must pass |
| Removed test | Present only at the baseline | Must be one this design retires. The default plan retires none |
| Pre-existing failure | Fails in both trees | Recorded in the task report and not fixed under this spec |

A failing test is re-run twice in both trees before it counts as a new failure.

### Per-area suites

New files are marked "new".

| Area | Lane | Suites |
|---|---|---|
| R6 | Portal, container, infrastructure | New `edge-cv-portal/backend/tests/test_jwt_authorizer_verification.py`; `test/backend-test/security/preservation/test_preservation_secrets_jwt.py`; `test/backend-test/security/test_secrets_bug_condition_exploration.py`; `test/backend-test/security/preservation/test_preservation_iam_out_of_scope_guard.py`; new `edge-cv-portal/infrastructure/test/jwt-authorizer-audience.test.ts` |
| R7 | Portal, container | `edge-cv-portal/backend/tests/test_export_checkpoint_static.py`; `test/backend-test/workflow_engine/test_workflow_python_bridge*.py`, `test_property_python_source_explicit_caps.py` and new `test_python_bridge_handler_containment.py`; `test/backend-test/utils/test_dda_user_management_utils.py` and `test_user_group_management_utils.py`; `test/backend-test/resources/test_image_source_accessor.py` and `test_workflow_accessor.py`; `test/backend-test/camera_sync/` |
| R9 | Container | `test/backend-test/workflow_engine/test_payload_fetch.py` and `test_bedrock_payload_reference.py`; new `test/backend-test/security/test_register_vllm_models_portal_api.py` |
| R10 | Container | New `test/backend-test/lyra/test_reference_image_map_migration_confinement.py`; `test/backend-test/security/preservation/test_preservation_deserialization_roundtrip.py` |
| R12 | Container, in a 3.10 (JetPack 6) and a 3.11 LocalServer image | `test/backend-test/camera_discovery/`, including new `test_stable_id_goldens.py`; `test/backend-test/camera_sync/`; `test/backend-test/test_py310_compat.py` |
| R14, R15 | Infrastructure, gate, Portal | Every Jest suite, including new `security-scan-remediation-infra.test.ts` and the existing `camera-registry-infra.test.ts`, `camera-shadow-sync-provisioning.test.ts` and `user-admin-audit-grant.test.ts`; `test/backend-test/security/preservation/test_preservation_iam_cdk_synth.py`; `test/backend-test/security/test_iam_bug_condition_exploration.py`; `edge-cv-portal/backend/tests/test_property_session_policy_scoping.py` |
| `already-mitigated` controls | Portal, container | `test_dda_sam_worker_mask_utils.py`, `test_dda_grounded_sam_worker_utils.py`, `test_detection_training_shared.py` and new `test_vllm_fit_check_url.py` in `edge-cv-portal/backend/tests/`; `test/backend-test/portal_builds/test_branch_discovery_property.py` |
| Every task | Container, gate | The whole `test/backend-test/security/preservation` suite; the guard suite (`test_preservation_out_of_scope_guard.py` and `test_preservation_secrets_out_of_scope_guard.py`), run after `edge-cv-portal/infrastructure/cdk.out` is moved aside as the build steering describes |

The device-side suites run on both interpreters that LocalServer images use: 3.10 on JetPack 6 and 3.11 elsewhere (Requirement 16.5). For R12, Python 3.8 is covered by `ast.parse(..., feature_version=(3, 8))` and the fallback unit test, since no shipped image runs these modules on 3.8.

New test code follows two rules, so the rescan gains no `test-only` results (Requirement 5.3):

- No literal looks like a credential: no AWS key, GitHub token or signed-JWT format. No variable, attribute or keyword argument is named with `password`, `token` or `secret`.
- Subprocess calls use a literal argument list.

### Property-based tests

The Hypothesis tests back Properties 1 to 4 and 7, plus the model-id property of `test_vllm_fit_check_url.py`. Each runs with Hypothesis's default example count, inside its area's suite, and is part of the baseline comparison as a new test.

### Unit and example-based tests

The area sections give the example cases: tampered, expired, wrong-issuer and wrong-audience tokens for R6; escaping handler paths, symlinks and uid strings for R7; schemes, redirects and redaction for R9; refusal conditions and the CLI exit code for R10; golden ids for R12. The Jest invariants cover R14 and R15, and the gate tests cover the fixtures. Static AST checks pin the shapes the rescan depends on: no `verify_signature` set to `False`, a literal program in the export call, and `hashlib.sha1` only in `stable_hash.py`.

### Test placement

| New file | Location | Covers |
|---|---|---|
| `test_jwt_authorizer_verification.py` | `edge-cv-portal/backend/tests/` | R6, Property 1 |
| `jwt-authorizer-audience.test.ts` | `edge-cv-portal/infrastructure/test/` | R6 wiring |
| `test_python_bridge_handler_containment.py` | `test/backend-test/workflow_engine/` | R7, Property 2 |
| `test_register_vllm_models_portal_api.py` | `test/backend-test/security/` | R9 script |
| `test_reference_image_map_migration_confinement.py` | `test/backend-test/lyra/` | R10, Property 6 |
| `test_stable_id_goldens.py` | `test/backend-test/camera_discovery/` | R12, Property 7 |
| `security-scan-remediation-infra.test.ts` | `edge-cv-portal/infrastructure/test/` | R14, R15, Properties 8 and 9 |
| `test_vllm_fit_check_url.py` | `edge-cv-portal/backend/tests/` | The `already-mitigated` control in `vllm_fit_check.py` |
| New tests in `test_preservation_iam_cdk_synth.py` | `test/backend-test/security/preservation/` | `test_post_fix_rescopes_only_narrow`, `test_refreshed_resources_match_synth`, `test_unfixed_snapshots_unchanged` |

New cases also go into the existing `test_dda_user_management_utils.py`, `test_user_group_management_utils.py`, `test_image_source_accessor.py`, `test_payload_fetch.py` and `test_export_checkpoint_static.py`.

### Device and account verification

The owner runs these, because this pipeline has neither hardware nor accounts.

On devices: the build steering requires on-device verification before an on-device change is committed, on every architecture it touches. That means JetPack 5 and JetPack 6, plus JetPack 7 where a device is available. After deploying the built LocalServer component:

- Run the harness stages `test_00_health.py`, `test_30_workflows.py` and `test_35_stream_cameras.py` from `test/on-hardware/harness/stages/`.
- Create a Folder image source under `/aws_dda/images`, which is accepted. Through the device API, create one at `/etc/x`, which returns HTTP 400.
- Run a workflow with a custom Python node.
- Run a `bedrock_inference` node with an `https` payload reference, which is fetched. Run one whose URL redirects to `http`, which is refused, with the URL redacted in the run log.
- Compare the camera list's `camera_source_id` values before and after the upgrade. They must be identical.
- If the station holds a legacy reference-image map under its artifact root, record the file's owner uid and mode (`stat -c '%u %a' <file>`), then run the migration CLI on it. A refusal with `Owned by uid N` means Greengrass gave the file to the component's run user, which decision 13 settles.
- Confirm the backend stays healthy for a sustained period, with no restart.

In accounts: the post-deploy checks in [Deploy order](#deploy-order), and one detector conversion job on the rebuilt export image's digest.

### Rescan with the pinned scanners

The local scanners come from the triage virtualenv outside the repository, `<triage-venv>`:

| Scanner | Version | Scope | Settings |
|---|---|---|---|
| Bandit | 1.8.6 | Every tracked `*.py` (`git ls-files '*.py'`) | `-t B102,B104,B105,B106,B107,B301,B307,B310,B324,B403,B604,B608 -f json`, with markers honored as the platform appears to. A second run adds `--ignore-nosec`. Compared by the matcher's key, the results it adds must be the 13 marked results at `4a3f960` plus any marker the owner approved. Anything else is a new `# nosec` that Requirement 18.4 doesn't allow |
| Semgrep OSS | 1.86.0 | Every tracked file in the languages of the 8 exported rules | The 8 exported rules, copied once from the public rules repository at a recorded commit into the triage workspace, then `--config <dir> --metrics=off --disable-nosem --json`. Copying the rules is the only network step, and it sends no project content. Rule ids are normalized by dropping the repeated final segment, so `...dangerous-subprocess-use-audit.dangerous-subprocess-use-audit` becomes `...dangerous-subprocess-use-audit` |
| Checkov | 3.2.255 | Copies, outside the repository, of the two refreshed Baseline_Templates, the two Unfixed_Snapshots and a fresh fixture synth of both stacks | `--framework cloudformation -o json`, first with the seven exported check ids plus `CKV_AWS_7` and `CKV_AWS_33`, then with every check. Results are judged against the expectations in [R14 SQS tests](#r14-sqs-tests), which differ for the refreshed Baseline_Templates and the fresh synth. Any check that fails on the new key or alias is triaged before the platform rescan |
| Scanner-X | Not available locally | | Covered only by the owner's platform rescan |

Semgrep wasn't re-run during triage, so its local setup is calibrated first: a run on the baseline worktree must reproduce the 75 exported Semgrep findings under the matcher. Any difference is a rules or version difference. It is recorded with the run, and for the rules that differ only the platform rescan decides closure. Bandit and Checkov were calibrated during triage ([Resolving findings to tracked code](#resolving-findings-to-tracked-code)).

The owner runs the platform rescan and exports its HIGH findings, which stay outside the repository like the Scan_Export. The same matcher reads that export.

### Matching rescan results to the ledger

The platform assigns finding ids per scan, so the matcher doesn't use them. Each result gets a key of rule, path and content:

- Rule: the scanner's rule id after normalization, Bandit's test id, Checkov's check id or Scanner-X's rule id.
- Path: the repository-relative path. A platform result that names only a basename resolves to the tracked file whose content key matches, as in triage.
- Content: for code, HTML and Dockerfiles, the flagged line with its surrounding whitespace stripped and internal runs of whitespace collapsed. For a template finding, the logical id of the resource whose JSON span holds the line. Regenerated templates move lines but keep logical ids.

Each ledger entry gets the same key, computed from its `resolved_path` and `resolved_line` at `4a3f960`. Within each key, results and entries pair one to one, nearest line first. That handles the 13 locations with more than one finding (28 entries) and identical lines within a file.

| Outcome | Meaning | Action |
|---|---|---|
| Pairs with a `FALSE_POSITIVE` entry | The entry still closes it (Requirement 17.5) | None. For an `already-mitigated` entry whose file changed since `4a3f960`, the cited control is re-read |
| Pairs with a `REMEDIATE` entry on the expected list below | The rule still matches the fixed code by construction | A Rescan_Record entry with the planned Disposition ([PRC-1 Rescan record](#prc-1-rescan-record)) |
| Pairs with a `REMEDIATE` entry not on that list | The fix didn't take | The task is reopened |
| Pairs with nothing | A new finding, or a flagged line whose content changed | Triaged under Requirements 1 to 4 into the Rescan_Record (Requirement 17.6) |
| An entry pairs with no result | Closed by the fix, for `REMEDIATE`; the line went away, for `FALSE_POSITIVE` | None for `REMEDIATE`. For `FALSE_POSITIVE`, the matcher reports why |

The scan closes when every result is in the first, second or fourth row with a recorded disposition, and none is in the third.

Eleven `FALSE_POSITIVE` entries sit in test files this spec edits. Their lines may move, which the matcher handles, and the tasks keep their text unchanged:

- `1338aa63-74cd-4b6f-b52c-58e0121faf2f-0` to `-4`, in `test/backend-test/security/test_secrets_bug_condition_exploration.py`.
- `389d8d79-a874-4aab-822c-36676b8eb465-0` to `-3`, in `test/backend-test/security/preservation/test_preservation_secrets_jwt.py`.
- `fd4c3597-737d-441f-ab0f-ed7a10e4e8df-0`, in `test/backend-test/security/preservation/_iam_preservation_support.py`.
- `a16452af-3065-45d9-8c7c-1ff8963b7a62-0`, in `test/backend-test/security/preservation/test_preservation_iam_cdk_synth.py`.

If a task must change one of those lines, its result lands in the fourth row and keeps the same reason.

### Expected rescan results

| Area | `REMEDIATE` findings | Expected on rescan | Planned record |
|---|---|---|---|
| R6 | 1 | Clears | |
| R7 export floor run | 2 | Clear. For `-1`, that assumes the taint rule's sink skips a list whose program is a literal (below) | If `-1` stays: `already-mitigated`, citing the literal program and the absolute-path check |
| R7 `python_bridge.py` | 1 | Still reported, because the program is `sys.executable` | `already-mitigated`, citing `_artifact_handler_path` |
| R7 `utils.py` | 1 | Still reported, because the argv is a variable and `utils.py` doesn't change | `already-mitigated`, citing `confine_dda_path` and the uid and gid check |
| R9 | 2 | Clear | |
| R10 | 6 | Still reported, because the rules flag the import and the load themselves | `already-mitigated`, citing `open_trusted_legacy_map` |
| R12 | 2 | Both clear. One new `B324` appears on the fallback line in `stable_hash.py` | `scanner-misread`, or an approved `# nosec B324` (decision 11) |
| R14 SNS | 2 | Clear. Only the platform runs `scanner-x/sns-topic-encryption` | |
| R14 SQS | 6 | The auto-label pair clears. Checkov 3.2.255 still reports the four SSE-SQS queues | `scanner-misread`, citing `SqsManagedSseEnabled: true` and the check's source, unless decision 5 picks a key |
| R14 DynamoDB | 2 | Clear | |
| R15 | 5 | Clear | |
| `FALSE_POSITIVE` | 411 entries | Still reported | Their own entries |

So 18 of the 30 `REMEDIATE` findings clear, 12 still pair with their entries, and 1 is new. The expected HIGH total is 424: Bandit 315, Semgrep OSS 72, Checkov 19 and Scanner-X 18. A newer platform scanner may differ, for example a Checkov release that reads `SqsManagedSseEnabled`. The matcher's outcomes decide closure, not these totals.

One expectation rests on an assumption that nobody has checked, because Semgrep wasn't re-run during triage. After the R7 fix, `WORK`, read from `EXPORT_WORK_DIR`, still feeds the ONNX, feeds and results paths into the export call's argv. So `feb1c815-fb14-44af-b148-6502a6f1fad8-1` clears only if the taint rule's sink, like the audit rule, skips a list whose program is a literal. The local Semgrep run on the fixed tree, after its calibration, settles it. If the finding stays, it pairs with its entry and gets the `already-mitigated` Rescan_Record entry in the table above. The counts then become 17 cleared and 13 paired, and the expected total becomes 425, with Semgrep OSS at 73.

### Unfixed_Snapshot findings

All 16 are `test-only`, and they stay closed for five reasons:

1. The files don't change (Requirement 16.4). The new `test_unfixed_snapshots_unchanged` compares each file's sha256 with `unfixed_snapshot_sha256` in the post-fix record, captured at `4a3f960`. The matcher also refuses to run unless `git diff --quiet 4a3f960 --` passes for both files.
2. The reason stays true. At `4a3f960`, the only code that references `.unfixed.template.json` is `test_preservation_iam_cdk_synth.py`; no deploy script or CDK app does. The matcher repeats that `git grep` on every run.
3. The scanners keep reporting them, and each pairs with its entry by rule, file and logical id:

   | Unfixed_Snapshot | Logical id | Findings |
   |---|---|---|
   | ComputeStack | `TrainingAlertTopic5C2CFA97` | `CKV_AWS_26` `950b9d60-0315-4329-9633-02b8aa44298c-10`, `scanner-x/sns-topic-encryption` `-0` |
   | ComputeStack | `CameraShadowReportDLQ50DB798A`, `CameraShadowReportQueue78573A06`, `AccountSyncAckDLQ75DF4763`, `AccountSyncAckQueue772F11D7` | `CKV_AWS_27` `-11`, `-12`, `-13`, `-14` |
   | ComputeStack | `EdgeCredentialsTable60F80243`, `AccountSyncTable789D4162` | `CKV_AWS_119` `-8`, `-9` |
   | ComputeStack | `StationProvisioningRoleDefaultPolicyD9DAB7DD` | `CKV_AWS_109` `-3`, `CKV_AWS_111` `-5` |
   | ComputeStack | `DevicesRoleOverflowPolicy11505496F` | `CKV_AWS_111` `-4` |
   | ComputeStack | `EnableSageMakerEventBridgeServiceRoleDefaultPolicy6B859A55` | `CKV_AWS_111` `-7` |
   | ComputeStack | `DeviceRegistrationsRoleDefaultPolicyFFA79FC4` | `CKV_AWS_107` `-1`, `CKV_AWS_108` `-2`, `CKV_AWS_111` `-6` |
   | UseCaseAccountStack | `DDASageMakerExecutionRoleDefaultPolicyAABC5DB6` | `CKV_AWS_111` `e559a804-79f1-4597-a014-0019733518c3-0` |

4. The drift test keeps reading them unchanged, by reversing the recorded rescopes ([How the synth gate, Baseline_Templates and Unfixed_Snapshots interact](#how-the-synth-gate-baseline_templates-and-unfixed_snapshots-interact)).
5. Any extra result on either file, for example from a new rule version, is triaged under Requirement 17.6 as `test-only` with the same reason.

## Observations

These are real issues the triage and this design noticed that the scan didn't report. They are recorded only; this spec doesn't fix them, because anything outside the export is out of scope until it is exported.

### Cross-cutting observations

1. CloudFormation scan coverage. The platform's IaC rules read only committed templates, and the four committed templates are fixtures of two stacks: ComputeStack and UseCaseAccountStack, out of the 23 stack classes in `edge-cv-portal/infrastructure/lib/`. StorageStack, which holds most Portal tables and buckets, and AuthStack, FrontendStack, the API stacks, BuildFleetStack, DataAccountStack and the rest were never checked. Even the two covered stacks are scanned as captured, so resources added since the capture, such as `CognitoAdminActivityTopic`, aren't seen. A run of the pinned Checkov over a fresh synth of every stack would show what the scan couldn't.
2. Inline markers with no effect. Four exported Semgrep findings sit on lines whose `# nosem` markers the platform ignored ([What the scanning platform honors](#what-the-scanning-platform-honors)). Such markers suggest a suppression that doesn't exist on the platform. R6 deletes one of those lines, and the others stay as they are.
3. Shipped_Code under `test/`. `test/on-hardware/register_vllm_models.py` is an operator script that `README.md` tells users to run, yet it lives in a test directory. That makes any directory-based exclusion of `test/` unsafe, and it makes the script easy to misclassify. Moving it to a scripts directory would be a separate change.
4. Base-image overrides. The six JetPack Dockerfiles in `src/backend` and `src/edgemlsdk` take `BASE_REGISTRY`, but five Dockerfiles name their `nvcr.io` or Docker Hub registry directly:
   - The four in `edge-cv-portal/plugin-build-images/`: `Dockerfile.arm64_jp5`, `Dockerfile.arm64_jp6` and `Dockerfile.arm64_jp7` pull from `nvcr.io`, and `Dockerfile.x86_64_nvidia` from Docker Hub.
   - `src/backend/Dockerfile.x86_64_nvidia:21`, which pulls from Docker Hub.

   A deployer who mirrors those base images has to edit the five files. The test-sandbox image takes its `nvcr.io` Triton base through the `TRITON_IMAGE` build argument instead (`edge-cv-portal/test-sandbox/Dockerfile:17-18`).
5. Tag-only base images. The `nvcr.io` bases of the six `BASE_REGISTRY` Dockerfiles, and of `plugin-build-images/Dockerfile.arm64_jp7:10`, are pinned by digest. These non-ECR bases are pulled by tag alone:
   - `plugin-build-images/Dockerfile.arm64_jp5:7` and `Dockerfile.arm64_jp6:7` (`nvcr.io/nvidia/l4t-jetpack`).
   - `plugin-build-images/Dockerfile.x86_64_nvidia:8` and `src/backend/Dockerfile.x86_64_nvidia:21` (Docker Hub `nvidia/cuda`).
   - The test-sandbox's default Triton base (`edge-cv-portal/test-sandbox/Dockerfile:17`).

   If such a tag moves upstream, those builds pull a different image with no repository change. The public ECR bases are pulled by tag as well.
6. Unreported public-registry pulls. `src/backend/Dockerfile.x86_64_nvidia:21` pulls `nvidia/cuda` from Docker Hub, as `plugin-build-images/Dockerfile.x86_64_nvidia:8` does. The export reports the plugin image (`scanner-x/docker-image-source` `9d83ac4b-5f20-4fcc-b2d4-963e36c6dce5-0`) but has no finding for line 21; the platform may have filtered it. The test-sandbox's `nvcr.io` Triton base isn't reported either. If the rescan reports either line, the matcher sees a new finding. It is triaged under Requirement 17.6 and gets the same `open-source-constraint` disposition as the other 15, under owner rule 1.
7. The SDK tree inside flask-app images. `build-custom.sh:161` copies the whole `src/edgemlsdk` tree into the gitignored staging directory `src/backend/edgemlsdk/`. Every backend Dockerfile then copies that directory into the image (`COPY edgemlsdk ./edgemlsdk`, for example `src/backend/Dockerfile:142`). The SDK's sources, including test code such as `src/edgemlsdk/src/test/longevity/deploy.py`, therefore sit inert in every flask-app image. Under the glossary, files inside a container image count as Shipped_Code, even when they are tests. No Disposition depends on that. Apart from its Dockerfiles, the tree's only ledger entry is `B105` `e016aab1-e20b-4d8f-9869-dd48ad1232ed-0` at `deploy.py:199`, which is `scanner-misread` whether or not the file ships.
8. A maintainer script's remote shell command. In `datasets/sync_captures_to_s3.py`, `pull_frames` puts `--device-dir` unquoted into the shell command it sends over ssh (`cd {device_dir} && ...`, lines 68-71). Its ssh call also turns off host-key checking (`StrictHostKeyChecking=no`, line 74).
   - The values are the operator's own arguments, so no trust boundary is crossed today. Quoting the directory and dropping the host-key override would cost nothing.
   - The scan didn't report these calls, because their argv lists start with a literal program.
   - The script also hardcodes environment-specific defaults and examples: a lab device address and directory (lines 52-53), and bucket names in its usage text (lines 25-36). The owner may want neutral placeholders there before a public push.

   Ledger entry `005f8505-e7c2-4e22-8589-fca6e3f61b50-0`, the `scanner-misread` entry for the script's `run()` wrapper, refers to this observation.

### Observations in the area sections

| Area | Observations | Where |
|---|---|---|
| R6 | A stale claim in `user_admin.py:7` that the admin routes sit behind the authorizer. JWKS keys cached for the container's life, not the documented hour. The authorizer's role carrying shared table grants it never uses | [R6 observations](#r6-observations) |
| R7 | `chown` and `chmod` following symlinks, a check-to-use gap that needs local host access. The installer's recursive `chown` reaching `/aws_dda/system` on a re-run | [R7 observations](#r7-observations) |
| R9 | The `dda_frames` helper `_fetch_http`, which has the same redirect and echo pattern as `payload_fetch.py` | [R9 observations](#r9-observations) |
| R10 | The tracked test-sandbox staging tree lagging its sources in five files, by about 337 changed lines | [R10 observations](#r10-observations) |
| R14 SNS | `CognitoAdminActivityTopic` unencrypted, with an EventBridge publisher. `BuildAlertTopic` unscanned. A second training-alerts topic in a stack no app instantiates | [R14 SNS observations](#r14-sns-observations) |
| R15 | Quick Setup credentials missing three IoT read actions that `setup_station.sh` calls. A module comment in `session_policy.py` that overstates its narrowing. Tunnel and events actions that no code calls. Three actions kept on `'*'` without a live-account check. The ECR statement on `'*'`. The single-account script's own `DDASageMakerExecutionRole`, which R15 doesn't change | [R15 observations](#r15-observations) |

## Proposed requirement changes

The changes below were proposals for the owner. On 2026-10-04 the owner applied PRC-1, PRC-2, PRC-3 and PRC-6 to `requirements.md`, and PRC-4 and PRC-5 don't apply (decision 14); the checker verifies the edited file's hash. PRC-1 to PRC-3 are needed for the design as written. PRC-4 and PRC-5 apply only with the owner choices that need them, and PRC-6 is editorial.

### PRC-1 Rescan record

Requirements 1.1, 17.5 and 17.6 conflict:

- Requirement 1.1 allows the Triage_Ledger exactly the 441 export entries "and no other entries".
- Requirement 17.5 needs every remaining rescan finding to match a `FALSE_POSITIVE` or `IGNORED_IMPAIRS_FUNCTION` entry.
- Requirement 17.6 needs new rescan findings triaged.

The design expects at least 13 rescan findings that can't match such an entry: 12 that pair with `REMEDIATE` entries, whose rules keep matching the fixed code, and 1 new line ([Expected rescan results](#expected-rescan-results)). They need a place to be recorded.

Proposed changes:

- Glossary, Triage_Ledger: add "and its Rescan_Record, `rescan.json`, which holds one entry per finding of each rescan, keyed by rescan id and `finding_id`, with the same Disposition and Sub_Reason fields and a link to the export entry it pairs with, if any".
- 1.1: "THE Triage_Ledger SHALL contain exactly one export entry for each of the 441 `finding_id` values in the Scan_Export, and no other export entries."
- 17.5: "...SHALL match a `FALSE_POSITIVE` or `IGNORED_IMPAIRS_FUNCTION` export entry or Rescan_Record entry."
- 17.6: "...SHALL be triaged under Requirements 1 to 4, and recorded in the Rescan_Record, before the scan is closed."

The rejected alternative was re-dispositioning the 12 `REMEDIATE` entries. It would erase the record that those findings were real and fixed, and their links to the design and tasks (Requirement 1.11).

### PRC-2 Fixture process extension

Requirement 16.3 allows Baseline_Template and approval-file changes "only through the project's existing process". That process can't record a removal: the synth gate fails on one, and its message forbids regenerating the baseline. The scanners read the committed fixtures, though, so the 15 Baseline_Template findings stay open until the fixtures change.

Proposed 16.3: "THE remediation SHALL make CloudFormation changes in the CDK source and SHALL update Baseline_Templates and approval files only through the project's existing process, or through an extension of it that the owner approves before any fixture changes, with owner approval where either process requires it." The extension is the post-fix record path in [R14 and R15 fixtures and deploy constraints](#r14-and-r15-fixtures-and-deploy-constraints).

### PRC-3 Repository audit markers

Requirement 18.4 lists `# nosec` among the inline suppressions that need owner approval. The repository's own IAM audit, `test/backend-test/security/iam_audit.py`, also reads `// nosec` markers in CDK source, and R15 moves three of them onto the statements it splits ([R15 change](#r15-change)). No scanner reads those markers in TypeScript, but the wording of Requirement 18.4 covers them.

Proposed addition to 18.4: "A marker that only the repository's own audit tests read, moved with the statement it documents, is not a scanner suppression under this criterion." Without the change, the three moves need the owner's approval under decision 1.

### PRC-4 DynamoDB key if the cost is declined

Applies only if the owner declines decision 6. Requirement 14.5 requires a customer managed key, and Requirement 4.5 doesn't let cost alone justify ignoring a finding, so declining leaves 14.5 unmet. Proposed 14.5 for that case: "...SHALL use a customer managed KMS key, unless the owner declines its cost, in which case the tables keep DynamoDB's default encryption and the `CKV_AWS_119` findings are recorded as owner-accepted."

### PRC-5 Python 3.8 if the hash fallback is dropped

Applies only if the owner picks the keyword-only form in decision 11. Requirement 12.3 names Python 3.8, which no shipped image uses to run the camera modules ([R12 runtimes](#r12-runtimes)). Proposed 12.3 for that case: "...in a form that runs on every interpreter that runs the module in a shipped image."

### PRC-6 Editorial

The glossary defers two facts to the design, which has now confirmed them:

- Unfixed_Snapshot: "the pre-fix template of its stack, read only by `test_baseline_drift_confined_to_I1_I4` and never deployed".
- Device_Runtime: the JetPack 5 LocalServer image runs CPython 3.11, built from source, while the host's system Python is 3.8.

## Open decisions for the owner

This list consolidates [Owner decisions before implementation](#owner-decisions-before-implementation) and the options raised in the area sections, and gives a recommendation for each. Items 1 to 3 are the open questions in `requirements.md`. The owner decided every item on 2026-10-04. Each item ends with the answer and the number of the owner decision in `requirements.md` that records it; a plain 'decision N' in this design still means item N.

1. Suppressions (open question 1, Requirement 18.4). The options are inline markers, configuration excludes, or neither. Recommendation: neither ([False-positive handling](#false-positive-handling)). If the owner wants fewer platform results anyway, the only marker the export suggests the platform honors is Bandit `# nosec <test id>` carrying the ledger reason, applied only to lines the owner names. Decisions 5 and 11 offer marker variants that depend on this one. Decided 2026-10-04: neither (owner decision 1).
2. The topic's key (open question 2). Recommendation: the AWS managed key, once the three read-only checks in [R14 SNS change](#r14-sns-change) find no AWS service publisher in any portal account. If one exists, the topic takes a customer managed key and approval edit O1 applies. Decided 2026-10-04: as recommended (owner decision 2).
3. Scanner names in a public spec (open question 3). Recommendation: keep the names and rule ids of Bandit, Semgrep OSS and Checkov, which are public projects. Replace `Scanner-X`, its `scanner-x/...` rule names and `scanner-x/sns-topic-encryption` with neutral labels; `scanner-x/sns-topic-encryption` doesn't follow cfn-lint's published numbering, so it travels with the Scanner-X rules. The labels go into `requirements.md`, `design.md`, `ledger.json` and `ledger.md`, and the mapping stays next to the Scan_Export. Do it as the last step before a push the owner approves (Requirement 18.3), and give the checker the mapping. The owner edits `requirements.md` and re-records its hash. Decided 2026-10-04: as recommended (owner decision 3). Under this approval the label swap, the plan's last change, also covers `tasks.md` and `rescan.json`; it keeps the mapping next to the Scan_Export and gives the checker the mapping. The owner swaps `requirements.md` and re-records its hash, as this item says.
4. The JWT authorizer: harden or delete. Recommendation: harden ([R6 JWT authorizer](#r6-jwt-authorizer)). That meets Requirement 6 as written, touches no IAM fixture and keeps the path for custom identity providers. Delete only if the owner won't offer that path ([R6 owner option](#r6-owner-option-delete-the-authorizer)). In that case, do it together with the R15 fixture refresh, and give the post-fix record a `removals` section with its own test, because `test_post_fix_rescopes_only_narrow` covers only rescopes. Decided 2026-10-04: harden (owner decision 4).
5. The cross-account queue pairs: SSE-SQS or a KMS key. The options are:
   - (a) SSE-SQS, recording the four Checkov 3.2.255 results as `scanner-misread` in the Rescan_Record.
   - (b) One customer managed key for both pairs. UseCaseAccountStack deploys first in every use-case account, the key adds charges, and approval edit O2 applies ([R14 SQS owner option](#r14-sqs-owner-option-customer-managed-key)).
   - (c) SSE-SQS plus Checkov skip metadata on the four queues. That needs decision 1, and the platform may not honor it.
   - (d) The AWS managed key for the account-sync-ack pair only, if no use-case account will ever send acks. It clears two results but closes a path the queue policy documents.

   Recommendation: (a). Decided 2026-10-04: (a) (owner decision 5).
6. The DynamoDB key's cost (open question 2, Requirement 4.5). Recommendation: approve one customer managed key for both tables ([R14 DynamoDB CMK](#r14-dynamodb-cmk)). It costs one key-month per portal account and region, plus request charges, which stay low because DynamoDB caches each table's decrypted key. Approving this also approves edits A1 to A3. Declining leaves Requirement 14.5 unmet and needs [PRC-4](#prc-4-dynamodb-key-if-the-cost-is-declined). Decided 2026-10-04: approved (owner decision 2); PRC-4 doesn't apply.
7. The sandbox copy of the migration utility. The options are:
   - (a) Fix both copies: change `src/backend`, copy only that fixed file into the sandbox staging tree, and keep the two copies identical with a parity test on that file.
   - (b) Leave the sandbox copy out of the staging commands.
   - (c) Drop the utility from both trees, if no legacy maps remain in use.

   Recommendation: (a) ([R10 change](#r10-change)). A later full re-stage keeps the fix, because it copies the fixed `src/backend` file. Option (b) also needs the README's commands changed, or the next full re-stage puts the file back. Option (c) needs a new conversion path for the postprocessor's error message. No option re-stages the rest of the tree, which lags `src/backend` ([R10 observations](#r10-observations)). That is a separate change with its own sandbox verification. Decided 2026-10-04: (a) (owner decision 8).
8. The IAM fixture path (Requirements 15.6 and 16.3). Recommendation: approve the post-fix record path in [R14 and R15 fixtures and deploy constraints](#r14-and-r15-fixtures-and-deploy-constraints), with [PRC-2](#prc-2-fixture-process-extension). Without it, the 15 Baseline_Template findings stay on rescan even after the CDK fix. Decided 2026-10-04: approved (owner decision 6), with PRC-2 applied in `requirements.md`.
9. Approvals-file edits A1 to A3 (Requirement 15.6). Recommendation: approve them with decision 6 ([IAM approvals-file updates](#iam-approvals-file-updates)). Decided 2026-10-04: approved together with the key cost (owner decision 2).
10. Folder image-source roots (R7). Recommendation: confine the walk to `/aws_dda` as designed. If a station has a source created through the API outside it, move that source under `/aws_dda` rather than widening the area. Decided 2026-10-04: as recommended (owner decision 8).
11. The R12 fallback line. The options are (a) keep the fallback and record its one new `B324` as `scanner-misread`, (b) the same with an approved `# nosec B324` on that line, or (c) the keyword-only call, which needs [PRC-5](#prc-5-python-38-if-the-hash-fallback-is-dropped). Recommendation: (a). Decided 2026-10-04: (a) (owner decision 8); PRC-5 doesn't apply.
12. An empty allow-list in `payload_fetch.py` (R9). Recommendation: accept, as residual risk, that an empty `allowed_uri_prefixes` allows every remote source. Decided 2026-10-04: accepted (owner decision 7). The `bedrock_inference` parameter description says so and recommends setting prefixes ([Owner decisions before implementation](#owner-decisions-before-implementation)).
13. Legacy-map roots (R10). Recommendation: approve the two roots in `TRUSTED_LEGACY_MAP_ROOTS`. Widen them only for a location a station is known to use. If the device check in [Device and account verification](#device-and-account-verification) finds unarchived artifacts owned by the component's run user, the owner also decides whether to admit that uid for files under the artifact root. Decided 2026-10-04: the two roots are approved (owner decision 8). Admitting the component run user's uid stays open until the device check reports the artifact owner.
14. Requirement changes. Recommendation: approve PRC-1 to PRC-3 before closure, and PRC-4 or PRC-5 only with the choice that needs it. Decided 2026-10-04 (owner decision 8): PRC-1, PRC-2, PRC-3 and PRC-6 are applied in `requirements.md`; PRC-4 and PRC-5 don't apply.

## Requirement traceability

| Requirement | Addressed in |
|---|---|
| R1 Complete triage based on the code | [Triage method and results](#triage-method-and-results): [Resolving findings to tracked code](#resolving-findings-to-tracked-code) (1.3, 1.4, 1.6), [Deciding each Disposition](#deciding-each-disposition) (1.2, 1.5, 1.10, 1.12), [Template findings](#template-findings) (1.7 to 1.9), [Ledger checks](#ledger-checks) and [Results](#results) (1.1, 1.11). Rescan entries: [PRC-1](#prc-1-rescan-record) |
| R2 Open-source constraint | [Scope](#scope); [Candidates considered](#candidates-considered), row 12; [Breakdown by Sub_Reason](#breakdown-by-sub_reason). 2.4: the constraints in [Overview](#overview) |
| R3 Findings that are not real vulnerabilities | [Deciding each Disposition](#deciding-each-disposition), including 3.6's credential-format checks; [False-positive handling](#false-positive-handling) |
| R4 Ignored because remediation would impair function | [IGNORED_IMPAIRS_FUNCTION](#ignored_impairs_function); cost-only items (4.5) as [decisions](#open-decisions-for-the-owner) 2, 5 and 6 |
| R5 No credentials in source code | [Scope](#scope); [Deciding each Disposition](#deciding-each-disposition); the rules for new test code in [Per-area suites](#per-area-suites) (5.3) |
| R6 Verified tokens in the Portal authorizer | [R6 JWT authorizer](#r6-jwt-authorizer); [Property 1](#property-1-the-authorizer-allows-only-fully-verified-tokens); decision 4 |
| R7 Safe process execution | [R7 process execution](#r7-process-execution), with 7.4's table in [R7 externally influenced arguments](#r7-externally-influenced-arguments); Properties 2 and 3; decision 10 |
| R8 No dynamic code execution from data | [Scope](#scope); [R7 custom Python handler path](#r7-custom-python-handler-path) (8.1) |
| R9 Restricted URL fetching | [R9 URL fetching](#r9-url-fetching); [Already-mitigated findings](#already-mitigated-findings), including the new fit-check test (9.5); Properties 4 and 5; decision 12 |
| R10 Safe deserialization | [R10 deserialization](#r10-deserialization); Property 6; decisions 7 and 13 |
| R11 Parameterized SQL | [Scope](#scope); [Scanner-misread findings](#scanner-misread-findings) |
| R12 Hashes used for identifiers | [R12 identifier hashes](#r12-identifier-hashes) and [R12 runtimes](#r12-runtimes); Property 7; decision 11 |
| R13 Transport and network exposure | [Scope](#scope); [Candidates considered](#candidates-considered), row 2; [Scanner-misread findings](#scanner-misread-findings) |
| R14 Encryption at rest | [R14 SNS encryption](#r14-sns-encryption), [R14 SQS encryption](#r14-sqs-encryption) and [R14 DynamoDB CMK](#r14-dynamodb-cmk); [Keys and key policies](#keys-and-key-policies); Property 8; decisions 2, 5 and 6 |
| R15 Least-privilege IAM | [R15 least-privilege IAM](#r15-least-privilege-iam); [R14 and R15 fixtures and deploy constraints](#r14-and-r15-fixtures-and-deploy-constraints); [IAM approvals-file updates](#iam-approvals-file-updates) (15.6); Properties 9 and 10; decisions 8 and 9 |
| R16 Deployability and behavior preservation | 16.1: [R14 and R15 fixtures and deploy constraints](#r14-and-r15-fixtures-and-deploy-constraints). 16.2: [In-place updates](#in-place-updates), [Deploy order](#deploy-order) and [Rollback](#rollback). 16.3 and 16.4: [How the synth gate, Baseline_Templates and Unfixed_Snapshots interact](#how-the-synth-gate-baseline_templates-and-unfixed_snapshots-interact), [Unfixed_Snapshot findings](#unfixed_snapshot-findings) and [PRC-2](#prc-2-fixture-process-extension). 16.5: [R12 runtimes](#r12-runtimes) and [Per-area suites](#per-area-suites). 16.6: each area's "preserving function" subsection, and [Ledger follow-ups](#ledger-follow-ups) |
| R17 Verification and closure | [Testing strategy](#testing-strategy): [Baseline run at 4a3f960](#baseline-run-at-4a3f960) (17.1 to 17.3), [Rescan with the pinned scanners](#rescan-with-the-pinned-scanners) (17.4), [Matching rescan results to the ledger](#matching-rescan-results-to-the-ledger) and [Expected rescan results](#expected-rescan-results) (17.5, 17.6); [PRC-1](#prc-1-rescan-record); Property 11 |
| R18 Public repository hygiene | The constraints in [Overview](#overview); [Ledger checks](#ledger-checks) (18.1, 18.2); [False-positive handling](#false-positive-handling) and decision 1 (18.4); decision 3 (18.3 and scanner names); [PRC-3](#prc-3-repository-audit-markers) |

### Ledger follow-ups

These ledger changes go with the tasks:

- Deliberate changes (Requirement 16.6). Today, only the `payload_fetch.py` entry `f035ce5a-9b67-4aac-9ec0-b5f597f0fdcc-0` records a deliberate change, and only part of it. A `deliberate_changes` field, carried by the merge script and summarized in `ledger.md`, records these:
  - R6 `490a14e8-75e3-48e4-af23-ae95676b238c-0`: the four changes listed in [R6 preserving function](#r6-preserving-function).
  - R7 `24e13eb5-0be6-4586-abc1-9ca6aa3b9aab-0`: Folder sources outside the allowed area (outside or at `/aws_dda`, or in `/aws_dda/greengrass` or `/aws_dda/system`) get HTTP 400. A shadow workflow id whose results path resolves outside that area isn't created, and it also stops the ids after it in that document and the document's metadata writes ([R7 DDA permission walk](#r7-dda-permission-walk)). A uid or gid that is set but not numeric stops startup. R7 `69ef51f8-d65b-4291-a4ae-d447bbd2367d-0`: a handler path outside the artifact fails the run.
  - R9 `f035ce5a-9b67-4aac-9ec0-b5f597f0fdcc-0`: all three changes listed in [R9 payload reference fetch](#r9-payload-reference-fetch). R9 `95141da5-9489-42a9-a8d5-e066182a8f68-0`: the base URL must be `https`, and redirects aren't followed.
  - R10, all six entries: a legacy map outside the trusted roots, not owned by root or the effective uid, or writable by group or others is refused ([R10 preserving function](#r10-preserving-function)).
- Superseded remediation text. The reason of R6 `490a14e8-75e3-48e4-af23-ae95676b238c-0` summarizes the fix as keeping the unverified parse "only for kid and iss key selection". This design supersedes that: only the header is read before verification, and the unverified payload decode is deleted ([R6 change](#r6-change)). The reason is updated with the R6 task, and this design governs until then.
- The reason of `005f8505-e7c2-4e22-8589-fca6e3f61b50-0`:
  - It calls the script "neither Shipped_Code nor Test_Code", a class the glossary doesn't define. It is reworded in the glossary's terms. The script isn't Shipped_Code, because no artifact packages it and no documentation tells users to run it. It isn't Test_Code either, because it isn't a test suite, fixture or test-support module. Its `scanner-misread` disposition doesn't depend on the class.
  - Its closing "see observations" names [Cross-cutting observations](#cross-cutting-observations), item 8.
- `task_ref` on every `REMEDIATE` entry, once tasks exist (Requirement 1.11).
- `design_ref_note`: once the design parts are assembled, it says the anchors match `design.md` headings, which the checker's anchor check confirms.
- `rescan.json`: created at the first rescan ([PRC-1](#prc-1-rescan-record)).

## Change log

Two independent reviews of the ledger and this design found the dispositions, counts and template mappings sound. The changes they led to are listed below. None changes a Disposition, and none adds a user-visible change beyond the deliberate changes in [Ledger follow-ups](#ledger-follow-ups).

### First revision

| Point raised | Change |
|---|---|
| Triage observations missing from the design | Addressed. [Cross-cutting observations](#cross-cutting-observations) gains items 5 to 8: tag-only base images, the unreported public-registry pulls, the SDK tree inside flask-app images, and the remote shell command in `datasets/sync_captures_to_s3.py`, which ledger entry `005f8505-e7c2-4e22-8589-fca6e3f61b50-0` points to. Item 4 now counts five Dockerfiles that bypass `BASE_REGISTRY` |
| Summary statements that contradicted the area sections | Addressed in place. The [Overview](#overview) and [Results](#results) tables list the files each area edits, and `utils.py` is marked unchanged. The R6, R9 and R10 preserving-function text says the deliberate changes reach the ledger through [Ledger follow-ups](#ledger-follow-ups), not that the ledger records them today. Step 4 of the fixture path defers to edits A1 to A3, so the approvals file gets no new `approved_on` date. The R9 observation on `healthcheck.py:61` states its marker, and the later correction is gone. The owner decision on Folder roots matches the R7 design. The superseded R6 ledger reason is listed in [Ledger follow-ups](#ledger-follow-ups) |
| The fresh-synth Checkov expectation | Addressed. [R14 SQS tests](#r14-sqs-tests) states one expectation for the refreshed Baseline_Templates and another for the fresh synth, which also fails `CKV_AWS_26` on `CognitoAdminActivityTopic`. Any other failure is compared with a fresh synth at `4a3f960` |
| The taint finding after the export fix | Addressed in [Expected rescan results](#expected-rescan-results). `feb1c815-fb14-44af-b148-6502a6f1fad8-1` may remain, the local Semgrep run settles it, and it gets an `already-mitigated` record if it stays. Making `WORK` a literal wasn't adopted, because it would remove the `EXPORT_WORK_DIR` override only to satisfy a scanner |
| The legacy-map owner rule and Greengrass artifacts | Addressed. [R10 preserving function](#r10-preserving-function) marks the artifact owner as unverified, the device checklist records it, and decision 13 covers admitting the component user's uid |
| The tunnel `ListTagsForResource` scope | Addressed. The action stays on `'*'` with `OpenTunnel` and `ListTunnels`, as it is today, because the IAM data lists no tunnel resource type for it ([R15 change](#r15-change)). Property 9 and the post-fix record define `unscopable_actions` to match |
| The R6 walk's end state | Addressed. Step 4 of `validate_jwt_token` names `Invalid token signature`, and a key-id collision test asserts it ([R6 change](#r6-change), [R6 tests](#r6-tests)) |
| The single-account role variant | Addressed as an observation in [R15 observations](#r15-observations) |
| Probe evidence that wasn't kept | Addressed for the implementation. The probes can't be recovered. The Checkov runs in [R14 SQS tests](#r14-sqs-tests) repeat them on the real templates and keep their inputs and output with the triage evidence |
| The `vllm_fit_check` property wording | Addressed. The property checks the raw path prefix ([Already-mitigated findings](#already-mitigated-findings)) |
| An undefined code class in `005f8505-e7c2-4e22-8589-fca6e3f61b50-0` | Backlogged to [Ledger follow-ups](#ledger-follow-ups), because ledger reasons change with the tasks. The entry's `scanner-misread` disposition doesn't depend on the class |
| A host path in a public spec | Addressed. The triage virtualenv is written `<triage-venv>` |
| The title convention | Addressed. The title follows the project's `Design Document:` form |

### Second revision

| Point raised | Change |
|---|---|
| The R10 sandbox re-stage would ship unrelated drift | Addressed. [R10 change](#r10-change) copies only the fixed `reference_image_map_migration.py` into the sandbox staging tree, and the parity test covers that file. The current-behavior text no longer calls the staging tree a verbatim copy. Decision 7 and [Device and image rollout](#device-and-image-rollout) match, and [R10 observations](#r10-observations) records the five files that lag their sources. A full re-stage is a separate change with its own sandbox verification |
| An empty uid or gid at container start | Addressed. Step 4 of [R7 DDA permission walk](#r7-dda-permission-walk) checks the id inside the existing `if userid:` and `if groupid:` branches, so `None` and `""` keep today's behavior, and the tests cover both |
| One escaping shadow id stops the whole document | Addressed by describing the effect in the R7 error table and in [Ledger follow-ups](#ledger-follow-ups). Per-id handling in `_on_accepted` wasn't adopted, because it would treat this failure differently from every other create failure there |
| Redirect schemes that the standard library rejects first | Addressed. `_GatedRedirectHandler` also overrides `http_error_302` and binds it for every redirect status, so such a target gets a `PayloadReferenceError` that names the scheme and doesn't echo the target ([R9 payload reference fetch](#r9-payload-reference-fetch)). The redirect tests assert the message |
| R6 log levels under the kept catch-all | Addressed by correcting the table. The module's own denials log ERROR from the catch-all and then the handler's WARNING, as they do today ([R6 errors and validation](#r6-errors-and-validation)). Re-raising `AuthorizationError` ahead of the catch-all wasn't adopted, because it would change log output. Step 5 now names the error it raises |
| The count in candidates row 12 | Addressed. Row 12 names the same five Dockerfiles as observation 4 ([Candidates considered](#candidates-considered)) |
| The catalog's "default" folder | Addressed. The bullet gives the catalog's examples, and says that the node has no default and never reaches the walk ([R7 DDA permission walk](#r7-dda-permission-walk)) |
| Review labels in a public spec | Addressed. This section is a change log, without labels that point into review documents outside the repository |

### Test environment on the implementation host

The implementation run found the host short of what [Baseline run at 4a3f960](#baseline-run-at-4a3f960) assumes. Both trees still run in the same environment for every lane, and nothing here changes a Disposition or what users see.

| Point raised | Change |
|---|---|
| The Portal lane needs Python 3.10 or later | The host has only Python 3.8 and 3.9, and `pytest==9.0.3` needs 3.10 or later. The Portal lane runs for both trees in the local `dda-portal-test:py311` image (Python 3.11.16), with one virtualenv inside it, and no Python is installed on the host. The Portal Lambdas run Python 3.11 and 3.12. The IAM synth gate and the guard suite stay on the host, because the synth gate skips inside containers. This replaces "Portal lane, on the host" |
| A Python 3.10 image for the device-side suites | They run under `python3.10` in the local `dda/flask-app:1.0.46` image (Ubuntu 22.04). The local `dda/flask-app:arm64-1.1.0` is the fallback for a suite the first can't import for a reason unrelated to this change. Each suite uses the same image in both trees, the run records which image ran it, and no image is pulled, built or pushed. On-device JetPack 6 verification stays with [Device and account verification](#device-and-account-verification) |
| The baseline worktree path | The host's Docker daemon has its own `/tmp`, so a container lane would mount an empty directory at `/tmp/dda-baseline-4a3f960`. The baseline worktree sits in the triage workspace instead, still outside the working tree |
| The baseline worktree location in the implementation run | The run's tooling allows one worktree only, at `.worktrees/remediation-base` under the repository root, so the baseline tree sits there. It shows as untracked while it exists, and it is removed after each use together with its empty parent |
| The container lane's `/repo` mount | The repository root holds a tracked `__init__.py`, so pytest 9 collects the root as a package and puts the mount's parent first on `sys.path`. Under `/repo` that parent is the image root, whose own backend copy (`/app.py`, `/model` and others) shadows the mounted tree: 33 R7 tests and up to 19 camera-sync tests fail on stale imports. Both trees are mounted at `/w/dda`, with `PYTHONPATH` to match. This replaces "mounted at `/repo`" |
| Triton's library in the container lane | Modules that load the Triton bindings fail with `libtritonserver.so` not found, so the container lanes add `/opt/tritonserver/lib` to the image's `LD_LIBRARY_PATH`, for both trees |
| PyJWT in the `flask-app:latest` image | The image has none, so the PyJWT-backed tests under `test/backend-test/security` skip in the 3.11 container lane in both trees. A supplementary lane, the same image with PyJWT 2.15.0 (the layer's pin), runs the R6 suites, and they pass at the baseline. It keeps the R6 preservation repoints exercised |
| One pytest process per suite | Each container suite runs in its own pytest process with its own result file, inside one container per area and lane. The comparison script sums the files of each area and lane |

### Implementation run: R6 authorizer (task 1)

Task 1 found the points below in the code and the test environment. Row 1 adds one deliberate change, which the R6 ledger entry records. The others change only test mechanics, log levels or documentation.

| Point raised | Change |
|---|---|
| `construct_rsa_key` failed on every token at `4a3f960` | It called `public_key.serialize(...)`, which cryptography's RSA public key doesn't have; the method is `public_bytes`. Under the layer's cryptography 50.0.1, every token was denied with `Failed to construct RSA key` before its signature check. So [R6 current behavior](#r6-current-behavior) overstates the deployed function: the signature check never ran. The S5 preservation tests mock `construct_rsa_key`, so they didn't show it. Requirement 6.5 asks for valid tokens to be accepted, so task 1 calls `public_bytes()`. Tokens that pass every check are now allowed. The ledger entry records this as its fifth deliberate change. Nothing invokes the authorizer, so no client sees the change |
| Property 1's example count in the Portal tests | The Portal conftest's `portal-fast` profile lowers the default to 25 examples, or 100 with `HYPOTHESIS_PROFILE=ci`. Property 1 takes `max_examples` from Hypothesis's built-in `default` profile, so it runs Hypothesis's default count of 100 in every run, as [Property-based tests](#property-based-tests) asks |
| `importorskip` with `minversion` in the Portal tests | `pytest.importorskip("jwt", minversion="2.8")` imports `packaging.version`, and the Portal conftest puts `backend/functions`, which holds the Portal's own `packaging.py`, first on `sys.path`, so collection failed. The test imports PyJWT with `importorskip("jwt")` and skips the module when its version is older than 2.8. The verification run reports no skip |
| A non-string `kid` | PyJWT 2.15.0's `get_unverified_header` rejects it with `InvalidTokenError`, so that case logs only the handler's WARNING (`Invalid token: ...`), not the catch-all's ERROR that [R6 errors and validation](#r6-errors-and-validation) lists. The request is denied either way. A missing `kid` takes the catch-all path, as listed |
| The setup guide's troubleshooting list | It named `Key not found in JWKS` and `Untrusted issuer`, which the module no longer raises. Besides the three places [R6 change](#r6-change) names, task 1 updates that list with the current reasons. A user-pool access token carries `client_id` and no `aud`, so it gets the missing-`aud` reason; `Token is not an ID token` is listed for a token with an allowed `aud` whose `token_use` isn't `id` (task 1 review) |
### Implementation run: R7 process execution (task 2)
Task 2 found the points below. None adds a deliberate change beyond the ones the R7 ledger entries record.
| Point raised | Change |
|---|---|
| A handler path that the real-path call can't resolve | `os.path.realpath` raises `ValueError` for a path that holds a NUL. `_artifact_handler_path` treats that as outside the artifact and raises `CustomPythonNodeError` naming the node, so the run fails with `failing_node_id` set, as such a path did before through `handler not found` |
| The camera-registry sync's view of the Folder check | The sync agent creates and updates image sources through `ImageSourceAccessor` and records an accessor 400's message as the change's failure reason (`src/backend/camera_sync/agent.py:1005-1008`). A Folder change pushed from the Portal with a location outside the allowed area fails with that message. The rule is the one in [R7 DDA permission walk](#r7-dda-permission-walk); the ledger's deliberate change names this path so the owner sees it |
| The excluded Greengrass subtree | `confine_dda_path` takes `/aws_dda/greengrass` as the parent of `constants.DDA_GREENGRASS_ROOT_FOLDER` and resolves both excluded subtrees with `posixpath.realpath`, so a symlink into either one is refused as well |
| Properties 2 and 3's example count | The device-side conftests' `fast` and `engine-fast` profiles lower the default to 25 examples. Both properties take `max_examples` from Hypothesis's built-in `default` profile and run 100, as Property 1 does |
| A relative `EXPORT_WORK_DIR` | The floor run now fails such a job with `EXPORT_WORK_DIR must be an absolute path`. Nothing sets the variable: the Dockerfile doesn't, and the Portal's job environment excludes it (`detector_conversion.py:497-505`), so no conversion job sees the change |
| Task 2 review: the wording of the allowed area | The first two `deliberate_changes` on `24e13eb5-0be6-4586-abc1-9ca6aa3b9aab-0`, the shadow-id row in [R7 DDA permission walk](#r7-dda-permission-walk) and [Ledger follow-ups](#ledger-follow-ups) now state the implemented rule: refused when the path resolves outside or to `/aws_dda`, or into `/aws_dda/greengrass` or `/aws_dda/system`. A device-UI entry of `.` is refused; a shadow id such as `../captures` resolves to `/aws_dda/captures` and is still created, as at `4a3f960`. Wording only; the code is unchanged. Top-level files such as the authorization settings file are listed in [R7 observations](#r7-observations) for the owner |

### Implementation run: R9 URL fetching (task 3)

Task 3 found the points below. Row 1 departs from [R9 payload reference fetch](#r9-payload-reference-fetch) to keep Requirement 16.6; none adds a deliberate change beyond the ones the R9 ledger entries record.

| Point raised | Change |
|---|---|
| The opener's TLS context | The design gives the opener `HTTPSHandler(context=https_ssl_context())` for every fetch. At `4a3f960` a plain `http://` reference used urllib's default opener, so an `https` redirect hop verified against the image's system trust store; only `https://` references used the `certifi` context. The literal composition would switch that hop to `certifi`, which nothing records: on an image without a trust store such a redirect would start to succeed, and with a private CA in the system store it would start to fail. `_build_opener(allowed_prefixes, context)` takes the context `_fetch_http` already chose (`certifi` for `https://` sources, `None` for `http://`), so both cases behave as before. The opener-shape test checks both; `test_http_fetch_passes_bounded_timeout` keeps its `context is None` assertion and adds the `https` case |
| Where the userinfo check runs | Before the prefix check, in the dispatcher. After it, `https://user@allowed.example/x` with the prefix `https://allowed.example/` would be denied as outside the prefixes under its redacted form, which looks inside them |
| `ProxyHandler` in the handler list | urllib's `OpenerDirector.add_handler` keeps a handler only if it handles some protocol, so `ProxyHandler()` joins the chain only when a proxy is configured, as in `build_opener()`. The opener-shape test sets `https_proxy` before it checks the handler |
| What the exception-text scrub removes | Each sensitive part with the delimiter that sets it off in a URL: the user information and its credential half, each followed by `@`, the query after its `?` and the fragment after its `#`, each as written and as `repr` escapes it, in one pass; the delimiter stays and the value becomes `<redacted>`. `http.client` rejects a space or control character in the request path with an `InvalidURL` that quotes the path, query included, with `repr`; the user name goes with the whole user information, because Property 4 covers all user information. The `file://` authority named in the malformed-URI message is scrubbed the same way. Task 3's review: the first version replaced each value wherever it occurred, so a `?1` cache-buster turned `HTTP Error 401: Unauthorized` into `HTTP Error 40<redacted>: Unauthorized`; with the delimiter, unrelated text stays as it is, every echo of a URL part is still removed, and Property 4's check, whose generated values carry markers, is unchanged |
| Prefixes read once | `fetch_reference_bytes` turns `allowed_prefixes` into a tuple first, because the redirect handler checks the list again; a generator would otherwise be empty at the second hop and admit any target |
| Catalog baseline formatting | `src/backend/workflow_engine/vendor/README.md` says to re-dump with `ensure_ascii` at its default, but the committed `catalog_baseline.json` round-trips only with `ensure_ascii=False`, which task 3.5's scoped refresh used; the diff is the one description line |
| Property 4's example count and the 308 cases | Property 4 takes `max_examples` from Hypothesis's `default` profile (100), as Properties 1 to 3 do. The 308 cases exist only where the runtime follows 308, so the `py310` lane has two fewer tests and no skip |

### Implementation run: R10 deserialization (task 4)

Task 4 found the points below. As built after its review, none changes Property 6 or adds a deliberate change beyond the six R10 ledger entries' `deliberate_changes`.

| Point raised | Change |
|---|---|
| The open flags and the descriptor check | [R10 change](#r10-change) opens with `O_RDONLY \| O_NOFOLLOW \| O_CLOEXEC` and checks only `fstat`. Tasks 4.1, the later revision, adds `O_NONBLOCK`, so a FIFO is refused instead of blocking the open, and repeats the location check on the descriptor actually opened, through its `/proc/self/fd` target, so a parent directory swapped after `realpath` can't redirect the read. Built as tasks 4.1 says |
| Roots compared as written | The first version put each root through `realpath` before the comparison, as `payload_fetch._allowed_file_roots` does. Task 4's review found that this lets the trust anchor move: `/aws_dda` is mode 775 for `dda_system_group`, which `src/host_scripts/setup_dda_users.sh` gives `ggc_user`, the default component user, so a component could put a symlink in place of a root or one of its parents and make any directory trusted. That leaves only the owner rule, which [R10 change](#r10-change) says can't stand alone. Both checks now compare with the roots as written, as step 2 says, so a map reached through a symlinked root component is refused with `Outside the trusted roots`. No installer symlinks a root component; a station that does gets the refusal with the fix in its message, and decision 13 covers widening the roots. One test case covers a symlinked root, and another opens a final-component symlink so `O_NOFOLLOW` is exercised on its own |
| What isn't a refusal | A missing or unreadable file and `ELOOP` raise the open's `OSError`, and an unreadable `/proc/self/fd` raises its `OSError` after the descriptor is closed. `main` keeps its existence check and its `legacy map file not found` message. Only the four conditions raise `UntrustedLegacyMapError`, each named in the message with the fix: `Outside the trusted roots`, `Not a regular file`, `Owned by uid N, not root or the effective uid M` and `Group- or world-writable (mode 0NNN)` |
| The handle when the import fails | The trusted handle is opened before the unchanged `import dill` line. Where that import fails, as in the sandbox, which doesn't install the package, the handle is closed when the `ImportError`'s traceback is released; a `try` around the import would re-indent the `# nosem` line that tasks 4 keeps as it is |
### Implementation run: R14 SQS queues (task 7)
Task 7 found the point below. It changes no Disposition and adds no deliberate change: the six queues take exactly the properties and comments in [R14 SQS change](#r14-sqs-change).
| Point raised | Change |
|---|---|
| Why the AWS managed key adds no KMS statement | [R14 SQS encryption choice](#r14-sqs-encryption-choice) says CDK drops the grants on the imported `alias/aws/sqs`. In aws-cdk-lib 2.270.0 a `QueueEncryption.KMS_MANAGED` queue holds no key object at all: it only renders `KmsMasterKeyId: alias/aws/sqs`, and the queue's `grantOnKey` grants nothing when `encryptionMasterKey` is unset, so `grantSendMessages` and the `SqsEventSource` add only their SQS actions. The conclusion stands: no IAM statement changes, as the shared Jest file's `alias/aws/sqs` case and the before-and-after synth comparison show |

### Implementation run: R15 IAM scoping (task 9)

Task 9 found the points below. None changes a Disposition, a scope or what any role is granted: the four sites take the statements in [R15 change](#r15-change), and every action each role held stays granted.

| Point raised | Change |
|---|---|
| Which rendering the post-fix record's `added` statements take | ComputeStack turns `@aws-cdk/aws-iam:minimizePolicies` on in its own constructor (`compute-stack.ts:144`), so a fresh synth merges and sorts its statements: site 1's `'*'` statement arrives merged with `sts:GetCallerIdentity`, and site 2's with the role's other `'*'` actions. The Baseline_Template holds each source statement unminimized. The `added` statements were therefore rendered by building the stacks with that one flag off, outside the repository; at `4a3f960` that render reproduces the committed statements exactly. UseCaseAccountStack doesn't minimize, so its statements come from the fixture synth. The synth gate compares grant atoms, so the minimized synth still equals the refreshed Baseline_Template plus the approvals |
| Where each rescope applies | Each rescope in `iam_baseline_post_fix_changes.json` also names its `carrier`, the flagged logical id whose statement it replaces, so `regen_baselines.py` and the gate read it from the record instead of from this spec's ledger |
| A line reference in the site 4 comment | The comment cites the `dda-*` prefix note in `compute-stack.ts`. That note is at lines 390 to 399 at `4a3f960` and at 395 to 404 after task 1, so the comment cites `compute-stack.ts:395-404` |
| A new gate test that skips in the container lane | [Baseline run at 4a3f960](#baseline-run-at-4a3f960) says a new test must pass. `test_refreshed_resources_match_synth` skips inside the flask-app container, as the synth test does (Requirement 17.3), because the container can't run `cdk synth`. So its ComputeStack case is a new test that skips in the `every-task` container lane, where both `test_synth_iam_statements_match_fixed_baseline` cases already skip at the baseline. It passes, with no skip, in the gate lane on the host, where the synth gate's no-skip rule applies. Comparisons of that lane list it as a new test not passing, which is expected |

### Implementation run: R14 DynamoDB key (task 8)

Task 8 found the points below. None changes a Disposition, a scope or what any role is granted beyond the key grants in [R14 DynamoDB change](#r14-dynamodb-change): the key, both tables and `AccountTablesKeyAccess` take that section's code and comments.

| Point raised | Change |
|---|---|
| Which policies carry `grantReadWriteData`'s key grants | The key-access comment in [R14 DynamoDB change](#r14-dynamodb-change), kept verbatim in the code, says those grants go to the AccountSync and UserAdmin default policies, which update after the tables. CDK adds them to each role's default policy, but ComputeStack minimizes its policies, so in the synth they land in `AccountSyncRoleOverflowPolicy1760E5CF1` and `UserAdminRoleOverflowPolicy190F26973`. Both overflow policies also hold the table statements, so they still reference the tables and update after them, and the deploy order in [R14 DynamoDB preserving function](#r14-dynamodb-preserving-function) holds. No statement moves between carriers against Deploy A, so the owner's `cdk diff` before Deploy B shows these two policies, the new `AccountTablesKeyAccess` and the two tables' `SSESpecification` |
| How the `cdk diff` reviews judge IAM changes | The task 9 review found that Deploy A's diff also shows statements moving between DevicesRole's carriers, and merged `'*'` statements re-rendering with unchanged actions. The diff reviews in the owner-gated section and task 11.7 now name those moves and judge IAM changes per role at grant level, as the synth gate does |
| The key test's room for task 6.3 | The key case allows one other key, task 6.3's topic key, so a late 6.3 under owner-gated section 1 doesn't fail it |
| Which grants Property 8 counts (task 8 review) | The review found three grant forms the table matcher missed, none of which the stack has today: an ARN with a token partition (`arn:${AWS::Partition}:dynamodb:...`, what `Table.fromTableName` and `formatArn` render in the env-less test synth), a conditioned grant on `'*'`, and a policy on a role the template doesn't define. The matcher now also reads a table named by `Ref` inside an ARN, and the case requires every policy that grants either table to attach by `Ref` to the three roles alone. A probe fails Property 8 for each such form. The change is test-only Deploy B content, so only the Jest file's `deploy-b-sha256.txt` line is re-recorded. A3's `reviewed` text stays as approved in 8.9, and owner-gated section 3 says how to read its "no wildcard action" |

### Implementation run: final verification (task 11)

Task 11's read-only `cdk diff` found the points below. None changes a Disposition, a scope or what any role is granted: per role at grant level, the diff's IAM changes equal the `4a3f960`-to-head changes.

| Point raised | Change |
|---|---|
| Layer versions in the owner's diff | [In-place updates](#in-place-updates) says the diff shows no replaced resource. A changed `AWS::Lambda::LayerVersion` is always replaced: CloudFormation publishes a new version, and the functions that use it update in place. Task 3.5's catalog text replaces `WorkflowCoreLayer` and its copy in `EdgeCVPortalTestRunnerStack`, at owner-gated section 8, because `catalog/nodes.py` is in neither Deploy path list; in Deploy A, TestRunnerStack changes only `TestRunStepsHandler`'s code. Section 2's review allows this one kind of replacement, and section 8's review names both layers with that cause |
| Dependency stacks | `cdk deploy EdgeCVPortalComputeStack` also deploys StorageStack, AuthStack and TestRunnerStack. Only TestRunnerStack changes: its layer copy, and `TestRunStepsHandler`'s code from the shared `edge-cv-portal/backend/functions` asset |
| Hashes from the working tree | Git-ignored `__pycache__` directories reach the Lambda and layer assets, and `station_install/` file modes reach the Quick Setup bundle. A diff from a used checkout therefore also shows `ImagingLayer` replaced and a `QUICK_SETUP_BUNDLE_SHA256` change. A fresh worktree, which the owner deploys from, carries neither, the bundle only when git writes it under umask `0002`, as on this host: the bundle script fixes order, times and owners but not modes, so a worktree written under umask `022` gives a third `QUICK_SETUP_BUNDLE_SHA256`. The deployed `SharedLayer` matches neither a fresh `4a3f960` build nor this tree, so any deploy replaces it |

### Implementation run: rescan (task 12)

Task 12's local rescan settled the open expectation in [Expected rescan results](#expected-rescan-results) and found results that the expected totals leave out. None changes a Disposition or a closure rule: the matcher's rows decide closure, and the head run has no row-3 result.

| Point raised | Change |
|---|---|
| `feb1c815-fb14-44af-b148-6502a6f1fad8-1` | Cleared: the tainted-env-args rule's sink skips the export-floor call, whose program is the literal `/opt/ort-floor/bin/python`. 18 `REMEDIATE` findings clear (17 locally, plus `scanner-x/sns-topic-encryption` on the platform) and 12 pair, the 424 branch of the expected total |
| Results in this spec's new test files | Besides the one new `B324` on `stable_hash.py`, the head has three results in new Test_Code: `B324` at `test/backend-test/camera_discovery/test_stable_id_goldens.py:75` (5.3's Python 3.8 stand-in) and `:105` (5.4's reference digest), and `avoid-dill` at `test/backend-test/lyra/test_reference_image_map_migration_confinement.py:76` (4.4's legacy-map writer). `rescan.json` records each as `FALSE_POSITIVE` / `test-only`. The platform may report them too, so its total can exceed 424 by up to three (Bandit 317, Semgrep OSS 73); they land in row 4 and take the same records |
| Local-only `avoid-dill` results | Locally, `avoid-dill` also reports the three Test_Code `dill` calls that 12.4 found at `4a3f960` and the export lacks; `rescan.json` records them as `test-only`. For `avoid-dill`, only the platform rescan decides closure |
| Semgrep's file-size limit | Semgrep 1.86.0 skips files over 1,000,000 bytes: the same 9 in both trees (both ComputeStack templates, `src/frontend/package-lock.json` and six binaries). The 75 exported findings reproduce with the limit, so the head run keeps it |
