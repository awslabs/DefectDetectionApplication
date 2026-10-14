# Bugfix Requirements Document

## Introduction

A user compiled a vision model to the `onnx` target
(`https://d23v4ltibogb5x.cloudfront.net/models/f182a10d-0da7-420a-943c-c370da7ee623`,
use case bucket `ryvan-cookies`, account `164152369890`) and the export failed
to start with:

> `... is not authorized to perform: s3:ListBucket on resource: arn:aws:s3:::ryvan-cookies`

This reason was surfaced — and survived polling — **because** the
`.kiro/specs/onnx-compile-error-diagnostics/` fix is deployed: that spec's
requirement 2.20 asked that a start failure implicating the hardcoded
`DDASageMakerExecutionRole` be *named*, and its 3.22 deliberately kept the
role itself out of scope. This spec is the follow-up it called for. The
reported model/bucket is treated as the reported *instance*, not a
reproducible fixture: the defect is structural and reproduces for any use
case whose bucket falls outside the role's S3 grant.

**The structural defect.** The portal accepts ANY bucket name for a use case,
but the SageMaker execution role it hands to every job can only reach
`dda-*` / `sagemaker-*` buckets in the single-account setup:

- `edge-cv-portal/backend/functions/usecases.py` requires only that
  `s3_bucket` be present at use-case creation (`required_fields = ['name',
  's3_bucket']`, ~line 1112); `list_s3_buckets` offers every bucket in the
  account, and the onboarding flow's `create_s3_bucket` (~line 879) creates a
  bucket with any user-supplied name — no `dda-` prefix is enforced,
  suggested, or documented at any of these three entry points.
- `edge-cv-portal/deploy-account-role.sh` (single-account path, `S3_POLICY`
  heredoc ~lines 211–262) grants `DDASageMakerExecutionRole` S3 actions only
  on `arn:aws:s3:::dda-*`, `dda-*/*`, `sagemaker-*`, `sagemaker-*/*`.
- `edge-cv-portal/backend/functions/compilation.py::_start_onnx_export_job`
  (~line 315; role ARN built at ~line 325) launches a SageMaker training job
  with `RoleArn = arn:aws:iam::{account_id}:role/DDASageMakerExecutionRole`,
  the `model` input channel pointed at the record's raw `artifact_s3` (in the
  use case's bucket), the sourcedir staged to
  `s3://{bucket}/models/onnx-export/{job}/sourcedir.tar.gz`, and
  `OutputDataConfig` in the same bucket. SageMaker validates the execution
  role's access to these locations at `CreateTrainingJob`, so the job fails
  at start for any bucket the role cannot reach.

**Why training previously succeeded on this same bucket.** Verified in git
history: commit `43ba6f2` (2026-07-09, "security: IAM least-privilege scoping
(findings I1–I17)") narrowed the `S3_POLICY` first statement from
`arn:aws:s3:::*` / `arn:aws:s3:::*/*` to the `dda-*`/`sagemaker-*` union
(finding I7). The existing training artifact under `ryvan-cookies` was
produced while the role still carried the pre-scoping wildcard; the ONNX
export is simply the first SageMaker job to exercise the role against a
non-`dda-*` bucket since the scoped policy was applied to the live account.
No bucket policy, alternate role, or artifact copy was involved — the repo
history fully explains the timeline, and the live failure confirms the
scoped policy is what the role carries now.

**Blast radius (verified in code).** Four launch paths pass the same role and
place inputs/outputs in the use case's bucket; all four break for a
non-`dda-*` / non-`sagemaker-*` bucket under the scoped single-account
policy:

1. **ONNX export** — `compilation.py::_start_onnx_export_job` (~line 315):
   input channel, sourcedir, and output all in `usecase['s3_bucket']`.
   *Confirmed broken live.*
2. **Neo compile** — `compilation.py::start_compilation_job` (~lines
   642–668): `InputConfig.S3Uri` is the repackaged
   `s3://{usecase_bucket}/models/.../model_for_compilation.tar.gz`,
   `OutputConfig` in the same bucket, same role ARN (~line 642). Same shape
   as the live failure; will fail identically. (The Lambda-side repackage
   upload itself succeeds — it runs under the portal/compute role, not the
   execution role; the live failure came only at `CreateTrainingJob`.)
3. **Training** — `training.py::create_training_job` (~lines 437–470): role
   at ~line 437, `OutputDataConfig` built from `usecase['s3_bucket']`, the
   manifest input wherever the data lives. Previously succeeded only under
   the pre-I7 wildcard; will now fail for a custom bucket.
4. **Ground Truth labeling** — `labeling.py::create_labeling_job`:
   `input_bucket = data_s3_bucket or s3_bucket` (~lines 287–298),
   `output_bucket = s3_bucket` (~line 302), `RoleArn` same role (~line 376
   region of the file, passed at ~line 457).

**The two provisioning paths already diverge.** The CDK multi-account stack
(`edge-cv-portal/infrastructure/lib/usecase-account-stack.ts`, ~lines
190–248) grants the same role S3 access on `arn:aws:s3:::*` **conditioned on
the bucket tag `dda-portal:managed = true`** — so in a CDK-provisioned
use-case account, a custom-named bucket can work if the operator tags it. The
single-account script has no equivalent mechanism and no documentation of
one. A third precedent exists for registration-time grants:
`edge-cv-portal/infrastructure/add-usecase-to-data-account.sh` appends the
role to a *data-account* bucket policy — but only for the separate
data-account flow, never for the use-case bucket itself.

**Operational context.** An immediate unblock is being applied by hand in
parallel: inline policy `UseCaseBucketAccess-ryvan-cookies` on the role in
account `164152369890`, scoped to `s3:ListBucket`/`s3:GetBucketLocation` on
the bucket and `s3:GetObject`/`s3:PutObject` on its objects. That patch lives
only in the console — no deploy artifact produces or tracks it. The durable
fix this spec specifies MUST make such hand-applied patches unnecessary going
forward.

**Candidate fix directions (design decides; NOT decided here).**
(a) grant the role access to registered use-case buckets via a managed
mechanism (policy update on use-case registration, or a documented setup
step — the CDK tag condition and the data-account bucket-policy script are
existing precedents); (b) stage SageMaker inputs/outputs into a `dda-*`
bucket the role already covers (code change, no IAM change); (c) document +
validate at use-case registration that custom buckets must grant the role
access, failing early with a clear portal error instead of a SageMaker start
failure. These are not mutually exclusive — (c)'s early validation is
compatible with either (a) or (b).

**Security posture is load-bearing.** The I7 scoping is a deliberate,
test-guarded security fix: `test/backend-test/security/
test_iam_bug_condition_exploration.py::test_i7_*` asserts the wildcard is
gone, and the preservation suite (`test_preservation_iam_shell_installers.py`,
`test_preservation_iam_pbt.py`, the `iam_baseline_heredoc_deploy-account-role_*.json`
and `iam_baseline_DDAPortalUseCaseAccountStack.template.json` goldens) pins
the current policy shapes. The fix MUST NOT reopen `arn:aws:s3:::*` without a
condition, and any intended change to a pinned policy goes through the
security gate's rebaseline + review protocol (`.kiro/steering/builds.md`),
not around it.

**Sibling-spec coordination.** `.kiro/specs/onnx-compile-error-diagnostics/`
is deployed and its contracts are preserved untouched by this spec: the
write-once `error`/`failure_reason` invariant, `classify_poll_kind` routing,
and the no-live-job markers are exactly what made this bug diagnosable, and
its task 11 records this failure reason and names this spec as the follow-up.
If design selects direction (b) (staging into a `dda-*` bucket), the sibling's
clause 3.5 ("identical `create_training_job` request") must be amended by a
coordination note there, since the S3 locations in the request would change.

## Bug Analysis

### Current Behavior (Defect)

**Registration accepts buckets the execution role cannot reach**

1.1 WHEN a use case is created or updated with an `s3_bucket` that matches
neither `dda-*` nor `sagemaker-*` THEN the system accepts it with no
validation, no warning, and no access grant — `usecases.py` requires only
that the field be present

1.2 WHEN the onboarding flow's `create_s3_bucket` is used to create the use
case's bucket THEN the system creates it under any user-supplied name,
without enforcing or suggesting a name the execution role's policy covers

1.3 WHEN `list_s3_buckets` populates the bucket picker THEN the system offers
every bucket in the account, including buckets the execution role cannot
reach, indistinguishably from ones it can

**Every SageMaker launch path fails at job start for such a bucket**

1.4 WHEN `_start_onnx_export_job` launches for a use case whose bucket the
role's policy does not cover THEN the system fails at `CreateTrainingJob`
with an authorization validation error (live instance: "not authorized to
perform: s3:ListBucket on resource: arn:aws:s3:::ryvan-cookies")

1.5 WHEN the Neo compile path calls `create_compilation_job` for such a use
case THEN the system passes `InputConfig.S3Uri` and `OutputConfig` in the
unreachable bucket with the same role, and the job fails

1.6 WHEN `training.py::create_training_job` launches for such a use case THEN
the system passes an `OutputDataConfig` (and, in the same-bucket data layout,
the manifest input) the role cannot reach, and the job fails — a path that
previously succeeded only because it ran before the I7 scoping

1.7 WHEN `labeling.py::create_labeling_job` launches for such a use case THEN
the system passes input and output S3 locations in the unreachable bucket
with the same role, and the job fails

**The failure surfaces late, inconsistently, and is patched by hand**

1.8 WHEN the incompatibility exists THEN the system surfaces it only at
SageMaker job start — potentially long after registration, after training
data has been uploaded and labeled — as a raw SageMaker validation string,
never as an early portal-level check

1.9 WHEN the operator compares provisioning paths THEN the system behaves
differently per path with no documentation: the CDK use-case-account stack
supports custom buckets via the `dda-portal:managed = true` tag condition,
while the single-account `deploy-account-role.sh` role has no mechanism at
all

1.10 WHEN an operator unblocks a stuck use case THEN the system offers no
managed mechanism, so access is granted by hand-applied console policy (the
live `UseCaseBucketAccess-ryvan-cookies` inline policy), untracked by any
deploy artifact and silently divergent from the committed policy

### Expected Behavior (Correct)

**A registered use case's bucket is reachable, or the mismatch is surfaced early**

2.1 WHEN a use case is registered with any bucket THEN the system SHALL
ensure — through a documented, deploy-artifact-managed mechanism — that
SageMaker jobs launched for that use case can read their inputs and write
their outputs, OR SHALL surface the incompatibility to the user as a clear,
actionable portal error naming the bucket and the role, before any SageMaker
job is attempted

2.2 WHEN `_start_onnx_export_job` launches for a use case whose bucket
satisfies the chosen mechanism THEN the system SHALL start the export
training job successfully, with no execution-role authorization failure on
the bucket

2.3 WHEN the Neo compile path launches for such a use case THEN the system
SHALL start the compilation job successfully under the same condition

2.4 WHEN `training.py::create_training_job` launches for such a use case THEN
the system SHALL start the training job successfully under the same condition

2.5 WHEN `labeling.py::create_labeling_job` launches for such a use case THEN
the system SHALL start the Ground Truth labeling job successfully under the
same condition

**Failures that remain are early, clear, and actionable**

2.6 WHEN a use case's bucket does NOT satisfy the mechanism THEN the system
SHALL report a portal-level error that names the bucket, the role, and the
remediation (how to make the bucket reachable, per the chosen mechanism) —
not a raw SageMaker `CreateTrainingJob`/`CreateCompilationJob` validation
string surfaced minutes later

**The mechanism is durable, scoped, and consistent**

2.7 WHEN the fix lands THEN the system SHALL make hand-applied per-bucket
console patches (the live `UseCaseBucketAccess-ryvan-cookies` inline policy)
unnecessary: the mechanism SHALL be expressed in committed deploy artifacts
(`deploy-account-role.sh`, `usecase-account-stack.ts`, portal code, and/or
setup documentation), and the existing hand patch SHALL be superseded or
absorbed by it

2.8 WHEN the mechanism grants IAM access THEN the grant SHALL be scoped to
the registered use-case bucket(s) — never a return to unconditioned
`arn:aws:s3:::*` — preserving the least-privilege posture of finding I7

2.9 WHEN an operator provisions single-account or multi-account THEN the
system SHALL provide a consistent, documented answer for custom-named
use-case buckets in both paths (today only the CDK path has the
`dda-portal:managed` tag mechanism, undocumented)

### Unchanged Behavior (Regression Prevention)

**`dda-*` and `sagemaker-*` use cases keep working unchanged**

3.1 WHEN any of the four launch paths (ONNX export, Neo compile, training,
labeling) runs for a use case whose bucket matches `dda-*` THEN the system
SHALL CONTINUE TO start the job successfully with the same role and the same
S3 locations as today

3.2 WHEN the role touches `sagemaker-*` buckets THEN the system SHALL
CONTINUE TO permit the same actions the current policy grants

**The security posture is not weakened**

3.3 WHEN the `deploy-account-role.sh` `S3_POLICY` and the CDK stack's role
grants are audited THEN the system SHALL CONTINUE TO contain no S3 statement
on unconditioned `arn:aws:s3:::*` — the I7 exploration test
(`test_i7_deploy_s3_policy_first_statement_wildcard`) SHALL CONTINUE TO pass

3.4 WHEN the security preservation suite runs THEN any policy shape this fix
intentionally changes SHALL go through the documented rebaseline + security
review protocol (`.kiro/steering/builds.md`), and every policy this fix does
NOT touch SHALL CONTINUE TO match its golden baseline byte-for-byte

3.5 WHEN the CDK multi-account path is provisioned THEN the
`dda-portal:managed = true` tag-conditioned grant SHALL CONTINUE TO work for
operators already relying on it

3.6 WHEN the separate data-account flow is used THEN
`add-usecase-to-data-account.sh` SHALL CONTINUE TO grant the role
bucket-policy access to data-account buckets exactly as today

**The diagnostics contracts stay intact**

3.7 WHEN a SageMaker job start fails for any remaining reason THEN the system
SHALL CONTINUE TO preserve the originating error write-once across polls,
classify entries via the no-live-job / `export_format` markers, and surface
the reason in both UI surfaces — the
`.kiro/specs/onnx-compile-error-diagnostics/` contracts and their test suites
SHALL CONTINUE TO pass unmodified

3.8 WHEN `_start_onnx_export_job` succeeds for an authorized bucket THEN the
system SHALL CONTINUE TO submit the same `create_training_job` request shape
as today — image, hyperparameters, channels, resources, stopping condition —
with S3 locations changing ONLY IF design selects the staging direction (b),
in which case the sibling spec's clause 3.5 SHALL be amended by a
coordination note rather than silently contradicted

**Registration and adjacent behavior**

3.9 WHEN a use case is registered with valid inputs THEN the system SHALL
CONTINUE TO require the same fields (`name` + `s3_bucket` single-account; the
full role/account set multi-account) and SHALL CONTINUE TO succeed for every
setup that works today

3.10 WHEN Greengrass devices access component artifacts and inference-results
buckets THEN the device-side policies (`DDAPortalComponentAccessPolicy`, the
token-exchange role grants) SHALL CONTINUE TO behave identically — this fix
concerns only the SageMaker execution role's use-case-bucket access

### Bug Conditions and Properties

**Key definitions.** `F` is the current (unfixed) system; `F'` is the fixed
system. `X` is a SageMaker job launch: a `(usecase, jobKind)` pair with
`jobKind ∈ {onnx-export, neo-compile, training, labeling}`.
`rolePolicyCovers(bucket)` is true when the bucket matches the role's
committed S3 grant (`dda-*` or `sagemaker-*` in the single-account policy).
`mechanismCovers(X)` is true when the chosen fix's mechanism makes the
bucket reachable (per design).

**Bug Condition:**

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type SageMakerJobLaunch
  OUTPUT: boolean

  // The use case's registered bucket is outside the execution role's S3
  // grant, no managed mechanism covers it (single-account today: none
  // exists), and the job places inputs or outputs in that bucket — so
  // SageMaker rejects the launch on the role's missing s3 permissions.
  RETURN NOT rolePolicyCovers(X.usecase.s3_bucket)
     AND NOT taggedForCdkGrant(X.usecase.s3_bucket)   // multi-account tag path
     AND jobUsesUsecaseBucket(X)                      // true for all four paths
END FUNCTION
```

**Property: Fix Checking** — for all launches where the bug condition holds,
the fixed system either starts the job (the mechanism made the bucket
reachable) or fails early with an actionable portal error; it never emits the
raw role-authorization start failure:

```pascal
FOR ALL X WHERE isBugCondition(X) DO
  result ← F'(X)
  ASSERT (mechanismCovers'(X) AND jobStarted(result))
      OR (portalError(result)
          AND names(result, X.usecase.s3_bucket)
          AND names(result, 'DDASageMakerExecutionRole')
          AND occursBefore(result, sageMakerCreateCall))
  ASSERT NOT rawRoleAuthStartFailure(result)
END FOR
```

**Property: Preservation Checking** — for all launches where the bug
condition does NOT hold (in particular every `dda-*`-bucket use case and
every tagged multi-account bucket), the fixed system behaves identically to
the original:

```pascal
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

**Property: Security Preservation** — no fix mechanism reopens the I7
wildcard:

```pascal
FOR ALL stmt IN s3Statements(DDASageMakerExecutionRole') DO
  ASSERT NOT (resourcesOf(stmt) ⊇ {'arn:aws:s3:::*'} AND conditionsOf(stmt) = ∅)
END FOR
```
