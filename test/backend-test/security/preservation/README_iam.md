# Preservation baseline tests — `security-iam-authorization-fixes` (I1–I18)

These tests implement **Property 2: Preservation — `F(X) = F'(X)` for every
legitimate (non-bug-condition) input** of the `security-iam-authorization-fixes`
bugfix spec (`bugfix.md` Req 3.1–3.18, `design.md` "Preservation Checking").

Methodology: **observation-first**. They capture the baseline behavior of the
seventeen in-scope IAM sites on the **UNFIXED** tree and assert the fixed tree
must match. They are written to **PASS on the unfixed tree** (task 2) and are
re-run after each fix wave (task 9) to confirm no legitimate behavior changed.

## How to run

From the repo root (no backend image required):

```
python3 -m pytest test/backend-test/security/preservation \
    -p no:cacheprovider --noconftest -v
```

`--noconftest` skips the heavy `test/backend-test/conftest.py`; these tests read
only source files / synthesized templates. Hypothesis is vendored under
`.hypothesis/`. Set `IAM_SKIP_CDK_SYNTH=1` to skip the `cdk synth` layer (the
source-level layer still runs).

## Files and what they cover

| Test file | Sites | Baseline artifact(s) |
| --- | --- | --- |
| `test_preservation_iam_cdk_synth.py` | I1–I6 | `iam_baseline_EdgeCVPortalComputeStack.template.json`, `iam_baseline_DDAPortalUseCaseAccountStack.template.json` (synth mode); `iam_baseline_ts_source_blocks.json` (source-level, always runs) |
| `test_preservation_iam_shell_installers.py` | I7–I14 | `iam_baseline_heredoc_*.json` (8 goldens) |
| `test_preservation_iam_json_template.py` | I15 | `iam_baseline_edge-device-iam-policy.json` |
| `test_preservation_iam_readme_prose.py` | I16, I17 | `iam_baseline_readme_prose.md` |
| `test_preservation_iam_out_of_scope_guard.py` | Req 3.18 | `iam_out_of_scope_baseline.json` |
| `test_preservation_iam_pbt.py` | PBT 1–4 | derived from the heredoc goldens |

Shared extraction helpers live in `_iam_preservation_support.py`.

## CDK synth vs source-level (I1–I6) — active mode

`cdk synth` **works** in this environment (aws-cdk 2.1033 + ts-node 10.9 via
`npx`, offline). The synth layer re-synthesizes the two stacks that ARE wired
into a synthesizable CDK app entrypoint and compares the emitted IAM
PolicyDocuments (deterministic — no asset hashes / timestamps) to the committed
templates:

* `EdgeCVPortalComputeStack` (I1, I2) — via the default `bin/app.ts` app.
* `DDAPortalUseCaseAccountStack` (I3, I4) — via `bin/usecase-account-app.ts`.

Fixture context: `portalAccountId=111111111111`, `externalId=fixture-eid`,
`trustedUseCaseAccountIds=222222222222,333333333333`, region `us-east-1`. The
`trustedUseCaseAccountIds` context is **not consumed** by the unfixed stacks
(that prop is added in task 7); passing it is harmless and keeps the fixture
identical to task 9's.

`LabelingWorkflowStack` (I5) and `TrainingWorkflowStack` (I6) are **not
instantiated in any synthesizable app entrypoint** in this repo, so they cannot
be diffed via `cdk synth --all`. Their targeted `sts:AssumeRole`
`PolicyStatement` blocks are captured at the **source level** in
`iam_baseline_ts_source_blocks.json` (alongside the I1–I4 blocks) and asserted
present verbatim on the unfixed tree. Synth-based diffing of I5/I6 is deferred
to task 9's environment / the CI gate (where the workflow stacks would be wired
up), per the design's Testing Strategy.

## Recorded baseline values (UNFIXED tree = wildcard `F(X)`)

- **I1/I2 (compute-stack):** the combined per-service statement and the portal
  S3 statement are on `resources: ['*']`; the `iam:PassRole` block is on
  `resources: ['*']` (condition preserved).
- **I3 (usecase Ground Truth):** S3 grant on `arn:aws:s3:::*` / `*/*`, no tag
  `Condition`.
- **I5/I6:** `sts:AssumeRole` on `arn:aws:iam::*:role/DDAPortalAccessRole`
  (labeling) and `resources: ['*']` (training).
- **I7:** `deploy-account-role.sh` `S3_POLICY` first stmt on
  `arn:aws:s3:::*`; the sibling `sagemaker-*` stmt preserved.
- **I8:** `SAGEMAKER_POLICY` on `"Resource": "*"`.
- **I9:** `AllowDDABucketPatternAccess` includes `arn:aws:s3:::*-dda-*`.
- **I10/I11/I12:** `create-edge-device-iam-role.sh` `greengrass:*` /
  `greengrassv2:*` / `iot:*` / S3 `"*"`.
- **I13/I14:** `launch-arm64-build-server.sh` `iot:*` and the S3 stmt bundling
  `s3:ListAllMyBuckets` on `"*"`.
- **I15:** `edge-device-iam-policy.json` `IoTDataPlane` on `"Resource": "*"`;
  the four preserved sids recorded byte-for-byte.
- **I16/I17:** README example fences carry `greengrass:*` / `iot:*` / S3 `*`;
  the surrounding prose is recorded excised for byte-for-byte comparison.
- **Req 3.18:** sha256 of 10 sibling-spec source files (findings #1–#8 / S1–S9)
  and a representative `cdk.out/asset.*` artifact.
