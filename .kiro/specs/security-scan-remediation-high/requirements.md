# Requirements Document

## Introduction

This spec remediates the findings of security scan `d5f01064-c0bb-4aac-91ff-5f55b65e2afd`, run against the Defect Detection Application on 2026-10-02 and exported on 2026-10-03. The export holds 441 findings from four scanners:

| Scanner | Findings |
|---|---|
| Bandit | 318 |
| Semgrep OSS | 75 |
| Checkov | 29 |
| Scanner-X | 19 |

Every finding has platform severity HIGH, so this spec covers that level only. A MEDIUM or LOW export would get a sibling spec with the same layout (`security-scan-remediation-medium`, `security-scan-remediation-low`). Four Scanner-X findings say "Severity: MEDIUM" in their description: two `scanner-x/sns-topic-encryption` and two `scanner-x/plaintext-http`. They stay in this spec because the platform severity field is authoritative.

The export reports `total_findings: 441` and is not truncated. Some finding-ID suffixes are missing, though; the `Dockerfile.jp5` IDs, for example, end only in `-0` and `-11`. The platform may have filtered other results, such as lower severities, before exporting. Anything not in this export is out of scope until it is exported.

The export lists file basenames only and names no commit. Each finding is resolved against the `remediation` branch, which was cut from `integration/all-specs` at `4a3f960` on 2026-10-03, so reported line numbers may have drifted.

The raw export stays outside the repository because its descriptions contain company-internal links. This spec refers to findings by rule id and `finding_id` only.

### Triage rules set by the project owner

1. **Not possible for open source: false positive.** A finding that can't be remediated in an open-source project built on public dependencies is a false positive; it is ignored, with the reason noted. The canonical case is the 15 `scanner-x/docker-image-source` findings ("Container Images Hosted in External Repositories"). Fixing them would require company-internal container registries that users of the public project can't reach.
2. **Would impair function: ignored.** Any other finding whose remediation would impair the system's function for users is ignored, with the reason noted.
3. **Everything else is remediated.**

Also agreed on 2026-10-03: findings that are not real vulnerabilities are closed as false positives, each with a reason checked against the code. These are scanner misreads, fake values in test-only code, vendored or untracked files, and code that an existing control already protects.

Issues that the triage notices but the scan did not report are recorded in the design as observations. They are not fixed under this spec.

### Finding inventory

"Test" means a `test_*.py` or `conftest.py` file and "support" means a `*_support.py` file. This split is by file name only. Each finding's Disposition is decided from the code (Requirement 1).

| Rule | Scanner | Count | Where (by file name) | Requirement |
|---|---|---|---|---|
| B105 hardcoded password string | Bandit | 252 | 221 test, 4 support, 27 other | 5 |
| B106 hardcoded password argument | Bandit | 11 | 8 test, 3 other | 5 |
| B107 hardcoded password default | Bandit | 1 | 1 support | 5 |
| detected-aws-access-key-id-value | Semgrep OSS | 2 | 2 test | 5 |
| detected-jwt-token | Semgrep OSS | 1 | 1 test | 5 |
| jwt-python-hardcoded-secret | Semgrep OSS | 1 | 1 test | 5 |
| unverified-jwt-decode | Semgrep OSS | 1 | `jwt_authorizer.py` | 6 |
| dangerous-subprocess-use-audit | Semgrep OSS | 62 | 47 test, 15 other | 7 |
| dangerous-subprocess-use-tainted-env-args | Semgrep OSS | 1 | `export_checkpoint.py` | 7 |
| B604 call with `shell=True` | Bandit | 1 | 1 test | 7 |
| B102 `exec` | Bandit | 13 | 13 test | 8 |
| B307 `eval` | Bandit | 3 | 3 other | 8 |
| B310 URL open scheme audit | Bandit | 10 | 8 other, 2 test | 9 |
| B301 pickle load | Bandit | 5 | 3 test, 2 other | 10 |
| B403 pickle-family import | Bandit | 7 | 4 test, 3 other | 10 |
| avoid-dill | Semgrep OSS | 2 | 2 other | 10 |
| B608 SQL string construction | Bandit | 6 | 6 test | 11 |
| sqlalchemy-execute-raw-query | Semgrep OSS | 5 | 5 test | 11 |
| B324 weak SHA-1 | Bandit | 5 | 3 test, 2 other | 12 |
| B104 bind to all interfaces | Bandit | 4 | 4 test | 13 |
| scanner-x/plaintext-http | Scanner-X | 2 | `index.html`, `triple.html` | 13 |
| scanner-x/docker-image-source | Scanner-X | 15 | 7 Dockerfiles | 2 |
| CKV_AWS_26 and scanner-x/sns-topic-encryption, SNS encryption | Checkov, Scanner-X | 4 | 2 Baseline_Template, 2 Unfixed_Snapshot | 14 |
| CKV_AWS_27 SQS encryption | Checkov | 10 | 6 Baseline_Template, 4 Unfixed_Snapshot | 14 |
| CKV_AWS_119 DynamoDB customer managed key | Checkov | 4 | 2 Baseline_Template, 2 Unfixed_Snapshot | 14 |
| CKV_AWS_107, 108, 109, 111 IAM | Checkov | 13 | 5 Baseline_Template, 8 Unfixed_Snapshot | 15 |

## Glossary

- **Scan_Export**: The export of scan `d5f01064-c0bb-4aac-91ff-5f55b65e2afd`, kept outside the repository.
- **Finding**: One entry of the Scan_Export, identified by its `finding_id`.
- **Triage_Ledger**: This spec's per-Finding record: `ledger.json` (machine-readable) and `ledger.md` (a summary grouped by rule), both in this directory. It also includes the Rescan_Record, `rescan.json`, which holds one entry per finding of each rescan, keyed by rescan id and `finding_id`. Each Rescan_Record entry has the same Disposition and Sub_Reason fields, plus a link to the export entry it pairs with, if any.
- **Disposition**: Exactly one of `REMEDIATE`, `FALSE_POSITIVE`, or `IGNORED_IMPAIRS_FUNCTION`.
- **Sub_Reason**: The kind of `FALSE_POSITIVE`:
  - `open-source-constraint`: the remediation needs company-internal infrastructure that users of the public project can't reach (owner rule 1).
  - `platform-constraint`: the platform doesn't support what the rule asks for, such as an IAM action that has no resource-level permissions.
  - `scanner-misread`: the flagged value or pattern isn't what the rule looks for. Examples are a configuration key name, a template delimiter, an empty default, or the name of a secret rather than its value.
  - `test-only`: the flagged code exists only in Test_Code, and the test controls its values and inputs.
  - `vendored-or-untracked`: the flagged file is third-party code copied into the repository, or git doesn't track it.
  - `already-mitigated`: the flagged code is Shipped_Code, and an existing control already prevents the issue.
- **Shipped_Code**: Code that reaches users or their devices. This covers:
  - Portal Lambda functions and layers, CDK stacks, and the Portal frontend.
  - The device web UI and HMI.
  - Greengrass components: LocalServer, model components, and plugin components.
  - Container images.
  - Scripts the documentation tells users to run, such as station install and deploy scripts.
- **Test_Code**: Test suites, fixtures, and test-support modules that no Shipped_Code artifact packages.
- **Baseline_Template**: A committed snapshot of a synthesized CloudFormation template in `test/backend-test/security/baselines/`, used by the IAM preservation tests. The two flagged ones are `iam_baseline_EdgeCVPortalComputeStack.template.json` and `iam_baseline_DDAPortalUseCaseAccountStack.template.json`. A Baseline_Template is a test fixture, but it mirrors what the CDK stacks deploy.
- **Unfixed_Snapshot**: The `.unfixed.template.json` twin of a Baseline_Template. It is the pre-fix template of its stack, is read only by `test_baseline_drift_confined_to_I1_I4`, and is never deployed.
- **Portal**: The edge-cv-portal cloud application: React frontend, Lambda backend, and CDK infrastructure.
- **LocalServer**: The Greengrass component that runs the device web UI, backend, and workflow engine on an edge device.
- **Device_Runtime**: The OS and Python that a device component runs on. JetPack 5 targets run Ubuntu 20.04, whose system Python is 3.8, but the JetPack 5 LocalServer image runs CPython 3.11, built from source. All other targets run Ubuntu 22.04 or newer.

## Requirements

### Requirement 1: Complete triage based on the code

**User Story:** As the project owner, I want every finding to have exactly one recorded disposition and reason, so that the scan can be closed and the result audited.

#### Acceptance Criteria

1. THE Triage_Ledger SHALL contain exactly one export entry for each of the 441 `finding_id` values in the Scan_Export, and no other export entries.
2. THE Triage_Ledger SHALL give each entry exactly one Disposition, plus a Sub_Reason when the Disposition is `FALSE_POSITIVE`.
3. THE Triage_Ledger SHALL record, for each entry, the rule id, the scanner, the reported basename and line, and the repository-relative path and line the Finding resolves to on the `remediation` branch.
4. IF a Finding does not resolve to a tracked file, THEN THE Triage_Ledger SHALL record `not-found` or `untracked` in place of the path.
5. THE triage SHALL decide each Disposition from the code at the resolved location, and SHALL NOT decide it from the rule id or file name alone.
6. WHEN several Findings report the same file and line, THE Triage_Ledger SHALL keep a separate entry for each and note the duplication.
7. WHERE a Finding is reported against a Baseline_Template, THE triage SHALL trace it to the CDK construct that produces the flagged resource and decide the Disposition from that construct.
8. WHERE a Finding is reported against an Unfixed_Snapshot, THE triage SHALL check the corresponding resource in the Baseline_Template and the CDK source, and SHALL cross-reference the Baseline_Template Finding for that resource when one exists.
9. IF an Unfixed_Snapshot Finding shows an issue that the deployed resource still has and that no Baseline_Template Finding covers, THEN THE design SHALL remediate it in the CDK source and THE Triage_Ledger entry SHALL link to that design section.
10. THE Triage_Ledger SHALL give every `FALSE_POSITIVE` and `IGNORED_IMPAIRS_FUNCTION` entry a reason specific to that Finding.
11. THE Triage_Ledger SHALL link every `REMEDIATE` entry to the design section that remediates it and, once tasks exist, to its task.
12. THE Triage_Ledger SHALL NOT copy Finding description text that contains company-internal links.

### Requirement 2: Open-source constraint (owner rule 1)

**User Story:** As the maintainer of a public open-source project, I want findings that can't be fixed with public dependencies recorded as false positives, so that the project isn't pushed onto infrastructure its users can't reach.

#### Acceptance Criteria

1. THE Triage_Ledger SHALL record each of the 15 `scanner-x/docker-image-source` Findings as `FALSE_POSITIVE` with Sub_Reason `open-source-constraint`, or `scanner-misread` where the flagged line does not pull an external image.
2. THE Triage_Ledger SHALL record, for each such Finding, the image reference at the flagged line and the public registry that serves it.
3. WHERE the only remediation for a Finding would add a dependency on company-internal registries, repositories, accounts, or services, THE Triage_Ledger SHALL record the Finding as `FALSE_POSITIVE` with Sub_Reason `open-source-constraint` and name that dependency.
4. THE remediation SHALL NOT point any Dockerfile, build script, or deployment at a company-internal registry or service.

### Requirement 3: Findings that are not real vulnerabilities

**User Story:** As the project owner, I want findings that aren't real vulnerabilities closed with a specific reason checked against the code, so that effort goes to real issues and every dismissal can be reviewed.

#### Acceptance Criteria

1. IF the flagged value or pattern isn't what the rule looks for, THEN THE Triage_Ledger SHALL record the Finding as `FALSE_POSITIVE` with Sub_Reason `scanner-misread` and state what the value is.
2. IF the flagged code exists only in Test_Code and the test controls its values and inputs, THEN THE Triage_Ledger SHALL record the Finding as `FALSE_POSITIVE` with Sub_Reason `test-only` and state why no shipped artifact includes the file.
3. IF the flagged file is third-party code copied into the repository, or git doesn't track it, THEN THE Triage_Ledger SHALL record the Finding as `FALSE_POSITIVE` with Sub_Reason `vendored-or-untracked` and name the file's origin.
4. IF the flagged code is Shipped_Code and an existing control already prevents the issue, THEN THE Triage_Ledger SHALL record the Finding as `FALSE_POSITIVE` with Sub_Reason `already-mitigated` and cite the path and line of that control.
5. IF the platform doesn't support the remediation the rule asks for, THEN THE Triage_Ledger SHALL record the Finding as `FALSE_POSITIVE` with Sub_Reason `platform-constraint` and name the limitation.
6. IF Test_Code contains a value in a real credential format, such as an AWS access key id, a GitHub token prefix, or a signed JWT, THEN THE triage SHALL confirm that the value isn't a live credential before recording the Finding as `FALSE_POSITIVE`.

### Requirement 4: Findings ignored because remediation would impair function (owner rule 2)

**User Story:** As the project owner, I want findings whose fix would break or degrade what users rely on to be ignored with the reason written down, so that security clean-up never quietly removes functionality.

#### Acceptance Criteria

1. IF every available remediation for a Finding would break or degrade a function users rely on, THEN THE Triage_Ledger SHALL record the Finding as `IGNORED_IMPAIRS_FUNCTION`.
2. THE Triage_Ledger SHALL state, for each `IGNORED_IMPAIRS_FUNCTION` entry, the function that would be impaired, the users or devices affected, and how the remediation would impair it.
3. WHERE a remediation exists that keeps the function working, THE Triage_Ledger SHALL record the Finding as `REMEDIATE` and use that remediation.
4. WHERE a compensating control or residual risk is known for an `IGNORED_IMPAIRS_FUNCTION` entry, THE Triage_Ledger SHALL record it.
5. THE triage SHALL NOT treat added cost alone as impaired function. It SHALL list a remediation that only adds cost as an owner decision.

These are cases to evaluate, not decisions:

- Changing a hash that derives persisted identifiers.
- Forcing HTTPS on a device-local endpoint that serves HTTP only.
- Using a Python feature that a Device_Runtime lacks.
- Encryption or IAM changes that would lock out an existing publisher, reader, or cross-account role.

### Requirement 5: No credentials in source code

**User Story:** As an operator, I want no real credential in the source tree, so that cloning or packaging the code never leaks access.

#### Acceptance Criteria

1. THE Shipped_Code SHALL NOT contain a hardcoded credential value: a password, token, API key, signing secret, or private key.
2. WHEN Shipped_Code needs a credential, THE Shipped_Code SHALL obtain it at runtime from a secret store or from configuration supplied at deploy time.
3. WHERE Test_Code needs a credential-shaped value, THE value SHALL be visibly fake and SHALL NOT be a real credential.
4. IF a flagged value is a real credential, THEN THE remediation SHALL remove it from the code and tell the owner to rotate it, because removing it does not revoke the copy in git history.

### Requirement 6: Verified tokens in the Portal authorizer

**User Story:** As a Portal user, I want API access decided only from tokens whose signature and claims have been verified, so that a forged token can't grant access.

#### Acceptance Criteria

1. THE Portal JWT authorizer (`edge-cv-portal/backend/functions/jwt_authorizer.py`) SHALL NOT allow a request, or derive identity, groups, roles, or use-case scope, from the claims of a token whose signature it has not verified against the issuer's published signing keys.
2. WHEN the authorizer reads a token before verifying it, for example to select a signing key by its `kid` header, THE authorizer SHALL use that content only for key selection.
3. THE authorizer SHALL verify the signature, expiry, and issuer, and SHALL check the audience or client id against the app clients the Portal allows, before it allows a request.
4. IF any of those checks fails, THEN THE authorizer SHALL deny the request.
5. THE behavior in criteria 1 to 4 SHALL be covered by automated tests. The tests SHALL show that tampered, expired, wrong-issuer, and wrong-audience tokens are denied and that valid tokens are still accepted.

### Requirement 7: Safe process execution

**User Story:** As a device operator, I want every process the system starts to be built from fixed programs and checked arguments, so that data from cameras, triggers, files, or API requests can't inject commands.

#### Acceptance Criteria

1. THE Shipped_Code SHALL start subprocesses with an argument list and without a shell, unless a shell is required and the command contains no externally influenced value.
2. WHEN a subprocess argument in Shipped_Code is derived from external input (an API request, a trigger payload, an environment variable, file content, or device configuration), THE Shipped_Code SHALL validate the value against an allow-list or a strict format before the call.
3. WHERE an externally influenced value is passed as a positional argument to a program that parses options, THE Shipped_Code SHALL keep the value from being read as an option, for example by rejecting a leading `-` or passing the value after `--`.
4. THE Triage_Ledger SHALL record, for each flagged call in Shipped_Code, which of its arguments are externally influenced.

### Requirement 8: No dynamic code execution from data

**User Story:** As a maintainer, I want no shipped code path to run data as code, so that a crafted input can never become executable.

#### Acceptance Criteria

1. THE Shipped_Code SHALL NOT pass externally influenced strings to `exec` or `eval`, except where running user-authored code is the documented purpose of the feature, such as custom Python nodes.
2. WHERE Shipped_Code needs to parse a Python literal from text, THE Shipped_Code SHALL use `ast.literal_eval` or a format-specific parser instead of `eval`.

### Requirement 9: Restricted URL fetching

**User Story:** As a Portal administrator and device operator, I want server-side URL fetches limited to the schemes and destinations each feature needs, so that a crafted URL can't read local files or reach unintended hosts.

#### Acceptance Criteria

1. WHEN Shipped_Code opens a URL that isn't a constant in the code, THE Shipped_Code SHALL allow only the schemes that feature needs and SHALL reject every other scheme, including custom schemes.
2. WHERE a feature is designed to read local files through `file:` URLs, THE Shipped_Code SHALL confine those reads to the feature's configured allowed paths or prefixes.
3. WHERE the URL comes from a user, a trigger payload, or an API request, THE Shipped_Code SHALL apply the feature's configured destination controls, such as allowed URI prefixes, before fetching.
4. IF a URL is rejected, THEN THE Shipped_Code SHALL fail with an error that names the rejected scheme or destination without echoing credentials embedded in the URL.
5. THE scheme restriction SHALL be covered by automated tests showing that unsupported schemes are rejected and that supported URLs still work.

### Requirement 10: Safe deserialization

**User Story:** As an operator, I want the system never to unpickle data someone else could have written, so that a crafted file can't run code.

#### Acceptance Criteria

1. THE Shipped_Code SHALL NOT load pickle or dill data from a location that a user, a remote system, or a less-privileged process can write.
2. WHERE Shipped_Code must read legacy pickle or dill data, such as during a one-time migration, THE Shipped_Code SHALL read it only from locations the component owns and SHALL NOT write new data in pickle or dill format.
3. WHERE code uses a pickle-family module only to inspect data, such as `pickletools` scanning a model checkpoint, THE code SHALL NOT construct objects from that data.

### Requirement 11: Parameterized SQL

**User Story:** As a maintainer, I want SQL built from bound parameters rather than string formatting, so that data can't change a query.

#### Acceptance Criteria

1. THE Shipped_Code SHALL pass values to SQL statements as bound parameters.
2. WHERE an SQL identifier, such as a table or column name, must be inserted into a statement, THE identifier SHALL come from a fixed allow-list in the code.

### Requirement 12: Hashes used for identifiers rather than security

**User Story:** As a maintainer, I want hashes that build identifiers to stay stable and be marked as non-security, so that no weak algorithm is relied on for security and existing identifiers keep working.

#### Acceptance Criteria

1. THE Shipped_Code SHALL NOT use SHA-1 or MD5 for a security purpose: authentication, tamper detection, or password storage.
2. WHERE SHA-1 or MD5 derives a non-security identifier, such as a deduplication key or a stable camera id, THE Shipped_Code SHALL keep the algorithm and its output unchanged, so that persisted identifiers stay valid.
3. THE Shipped_Code SHALL mark such a hash as non-security (`usedforsecurity=False`). It SHALL do so in a form that runs on every Device_Runtime that imports the module, including Python 3.8, which doesn't accept that keyword.

### Requirement 13: Transport and network exposure

**User Story:** As a station operator, I want pages and services to use encrypted transport and listen only where needed, wherever devices and browsers support it.

#### Acceptance Criteria

1. WHEN a page flagged by `scanner-x/plaintext-http` loads a script, style, image, or data over `http://` from a host that also serves HTTPS, THE page SHALL load it over HTTPS or a same-origin relative URL.
2. IF the flagged `http://` text isn't a network request, such as an XML namespace or a documentation link, THEN THE Triage_Ledger SHALL record the Finding under Requirement 3.
3. THE Shipped_Code SHALL listen on all interfaces (`0.0.0.0` or `::`) only where remote access is part of the feature, such as the device web UI, and SHALL otherwise listen on loopback.

### Requirement 14: Encryption at rest for topics, queues, and tables

**User Story:** As a Portal administrator, I want topics, queues, and tables encrypted at rest without breaking the services that publish to or read from them.

#### Acceptance Criteria

1. THE Portal CDK stacks SHALL enable server-side encryption on each SNS topic flagged by CKV_AWS_26 or `scanner-x/sns-topic-encryption`.
2. WHERE an AWS service, such as CloudWatch alarms or EventBridge, publishes to an encrypted topic, THE topic SHALL use a customer managed KMS key whose key policy lets that service publish, because the AWS managed SNS key does not allow service publishers.
3. THE Portal CDK stacks SHALL enable server-side encryption on each SQS queue flagged by CKV_AWS_27, including dead-letter queues.
4. WHERE an AWS service or another account sends messages to an encrypted queue, THE queue's encryption SHALL allow that sender.
5. THE DynamoDB tables flagged by CKV_AWS_119 SHALL use a customer managed KMS key, with key permissions for every principal that reads or writes them, including cross-account roles.
6. IF a principal that must use a topic, queue, or table can't be given access to its key, THEN THE Triage_Ledger SHALL handle the Finding under Requirement 4.

### Requirement 15: Least-privilege IAM

**User Story:** As a Portal administrator, I want each role's write, permissions-management, and data-access permissions limited to the resources it uses, so that a compromised function can't escalate privileges or move data out.

#### Acceptance Criteria

1. THE Portal CDK stacks SHALL scope each flagged IAM statement that grants write, permissions-management, or credential-exposing actions. Each statement SHALL be scoped to specific resource ARNs or ARN patterns, or constrained with conditions, wherever the resources are known or follow a pattern at synth time.
2. WHERE a flagged statement mixes actions with and without resource-level permissions, THE remediation SHALL keep `Resource: "*"` only for the actions without resource-level permissions and SHALL scope the rest under criterion 1.
3. IF every action that fails the check lacks resource-level permissions, THEN THE Triage_Ledger SHALL record the Finding as `FALSE_POSITIVE` with Sub_Reason `platform-constraint` and name those actions.
4. THE IAM changes SHALL NOT remove a permission that a deployed function, build project, device role, or cross-account role uses at runtime.
5. IF scoping a statement would remove such a permission, THEN THE Triage_Ledger SHALL handle the Finding under Requirement 4.
6. THE design SHALL list any planned change to the owner-approved `iam_post_fix_approved_additions.json`, and the owner SHALL approve it before it is made.

### Requirement 16: Deployability and behavior preservation

**User Story:** As a Portal administrator and device operator, I want the fixes to deploy cleanly onto existing installations and keep everything working as before.

#### Acceptance Criteria

1. THE CDK changes SHALL keep every stack within CloudFormation's template-size and resource-count limits. As of September 2026, ComputeStack's template was about 644 KB of the 1 MB limit, and ApiGatewayStack held about 491 of 500 resources.
2. THE encryption and IAM changes SHALL deploy as in-place updates that keep existing data, messages, subscriptions, and resource names.
3. THE remediation SHALL make CloudFormation changes in the CDK source and SHALL update Baseline_Templates and approval files only through the project's existing process, or through an extension of it that the owner approves before any fixture changes. Owner approval SHALL be obtained wherever either process requires it.
4. THE remediation SHALL NOT hand-edit an Unfixed_Snapshot.
5. THE device-side changes SHALL run on every supported Device_Runtime.
6. THE remediation SHALL NOT change user-facing behavior except where the Triage_Ledger records a deliberate change.

### Requirement 17: Verification and closure

**User Story:** As the project owner, I want proof that the fixes work and nothing regressed, and a rescan that confirms the scan is closed.

#### Acceptance Criteria

1. WHEN a remediation task is complete, THE affected test suites SHALL pass with no new failures compared with a baseline run at the branch point, `4a3f960`.
2. WHEN CDK code changes, THE infrastructure tests SHALL run after `npm run build` in `edge-cv-portal/infrastructure`.
3. WHEN CDK code changes, THE IAM preservation synth gate SHALL run on the host, because the gate skips inside the flask-app container.
4. WHEN all remediation tasks are complete, THE scan SHALL be re-run, with Bandit, Semgrep OSS, and Checkov run locally and the platform scan run by the owner.
5. WHEN the rescan finishes, every remaining finding SHALL match a `FALSE_POSITIVE` or `IGNORED_IMPAIRS_FUNCTION` export entry or Rescan_Record entry in the Triage_Ledger.
6. IF the rescan reports a finding that the Triage_Ledger doesn't cover, THEN THE finding SHALL be triaged under Requirements 1 to 4, and recorded in the Rescan_Record, before the scan is closed.

### Requirement 18: Public repository hygiene

**User Story:** As the maintainer of a public repository, I want this work to leak no internal information and not publish unfixed vulnerabilities early.

#### Acceptance Criteria

1. THE spec files and remediation changes SHALL NOT contain company-internal URLs, internal documentation or wiki references, internal account-system names, or internal chat channel names.
2. THE Scan_Export SHALL stay outside the repository.
3. THE `remediation` branch SHALL NOT be pushed to the public remote while this spec describes unremediated vulnerabilities, unless the owner decides otherwise.
4. THE remediation SHALL add inline scanner suppressions or scanner configuration excludes only after the owner approves that approach. Inline suppressions include `# nosec`, `# nosemgrep`, and Checkov skip comments. A marker that only the repository's own audit tests read is not a scanner suppression under this criterion, provided it moves with the statement it documents.

## Open questions for the design

1. **Suppressions.** False positives could get inline suppressions, scanner configuration excludes for test directories, or neither. The scanning platform may not honor inline suppressions. The design recommends an approach and the owner decides (Requirement 18.4).
2. **Key cost.** Customer managed KMS keys for the DynamoDB tables, and for the SNS topic if a service publishes to it, add monthly key and request charges (Requirement 14). The owner confirms before implementation.
3. **Scanner names in a public spec.** This spec names the scanners and their rule ids for traceability, including Scanner-X's `scanner-x/...` rules. Before pushing, the owner decides whether to keep those names or use neutral labels.

## Owner decisions

The owner recorded these decisions on 2026-10-04, after the design review. The design's "Open decisions for the owner" section has the reasoning behind each one.

1. **Suppressions:** none. False positives are closed through their ledger entries only.
2. **Key cost:** approved. Both flagged DynamoDB tables get one customer managed KMS key, along with approvals-file edits A1 to A3. The SNS topic keeps the AWS managed key unless the read-only checks find an AWS service publisher. If they do, the topic gets a customer managed key and approval edit O1 applies.
3. **Scanner names:** Bandit, Semgrep OSS, and Checkov keep their names. Scanner-X, its `scanner-x/...` rules, and `scanner-x/sns-topic-encryption` get neutral labels as the last step before any push. The mapping between the labels and the original names stays outside the repository.
4. **JWT authorizer:** harden it rather than delete it.
5. **SQS:** follow the design's recommendation.
   - SSE-SQS for the two queue pairs that take messages from other accounts: camera-shadow reports and account-sync acks, each with its dead-letter queue.
   - The AWS managed key for the auto-label pair, whose senders are all in the portal account.
   - Checkov 3.2.255 still flags the four SSE-SQS queues. Those results are recorded in the Rescan_Record as `scanner-misread`.
6. **IAM test baselines:** the post-fix record path in the design is approved as the extension in Requirement 16.3.
7. **Residual risk accepted:** an empty `allowed_uri_prefixes` keeps allowing every remote source in payload reference fetches (Requirement 9.3). Denying by default would break nodes that are configured without prefixes. The user-facing documentation states this and recommends setting prefixes.
8. **Everything else:** all other recommendations in the design stand. The proposed requirement changes PRC-1, PRC-2, PRC-3, and PRC-6 are applied in this document. PRC-4 and PRC-5 don't apply, given decisions 2 and the kept hash fallback.
