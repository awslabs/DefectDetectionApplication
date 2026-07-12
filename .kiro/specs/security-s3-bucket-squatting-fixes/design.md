# S3 Bucket Squatting Prevention (Group 5) Bugfix Design

## Overview

A code security review of the DefectDetectionApplication (DDA) — the AWS "DDA
Code Review" scan captured in `security-findings-report.json` (63 findings) —
surfaced six HIGH-severity **S3 Bucket Squatting** findings. This spec is the
fifth remediation group (stacked after `security-iam-authorization-fixes`); it
is scoped **strictly** to findings **B1–B6** across the longevity deploy
tooling, the SDK publish script, two Sphinx docs, and the repo-root SageMaker
notebook, plus the repo-audit gate (B7).

The defect, across six sites, is one shape: a hardcoded, predictable S3 bucket
name is read-from, written-to, or shown in copy-pasteable documentation /
notebook code **without** any bucket-owner assertion, integrity verification,
env-var parameterization (for team-owned writes), or placeholder. Because the
bucket names (`panorama-sdk-v2-artifacts`, `edgeml-sdk-docs`,
`edgeml-sdk-longevity-tests`, `lookoutvision-us-east-1-0e205be246`) are guessable
and the accesses are unverified, an attacker who squats a same-named bucket in a
region/account where the legitimate one does not exist could serve trojaned
`.deb`/`.whl` artifacts on the read path, receive build outputs on the write
path, or propagate the exposure into customer deployments via copy-pasteable
examples.

Per the user's **Option A** decision, the fix is owner-assertion / integrity
verification and parameterization, **NOT** a rename of the AWS-managed buckets
(DDA cannot rename a bucket it does not own):

1. **Read-path integrity (B1)** — `deploy.py` asserts the expected owner of
   `panorama-sdk-v2-artifacts` and `edgeml-sdk-longevity-tests` before the SSM
   `dpkg -i` / `pip install`.
2. **Write-path parameterization + owner assertion (B2, B3)** — `publish.sh`
   reads the target buckets from env vars (defaulting to the current values) and
   asserts the expected owner before each upload.
3. **Doc / notebook placeholder + verification note (B4, B5, B6)** — the SDK
   install docs, the message-broker config sample, and the training notebook are
   made placeholder-based and/or carry an ownership-verification note.

### Critical implementation decision — the `--expected-bucket-owner` mechanism (RESOLVED, with evidence)

The bugfix.md flagged as an implementation-time check *whether the high-level
`aws s3 cp` / `aws s3 sync` commands accept `--expected-bucket-owner`* (versus
only the low-level `aws s3api` commands). **This design resolves that question
concretely against the CLI installed in this environment, and the answer flips
the naive primary approach.**

Verification performed during design (AWS CLI **v2.35.19**, the current v2):

```
$ aws --version
aws-cli/2.35.19 Python/3.14.6 Linux/5.15.0-1092-aws exe/aarch64.ubuntu.20

$ aws s3 cp help   | grep -c expected-bucket-owner      # high-level  -> 0
$ aws s3 sync help | grep -c expected-bucket-owner      # high-level  -> 0
$ aws s3api head-bucket help | grep -c expected-bucket-owner   # low-level -> 1
$ aws s3api get-object  help | grep -c expected-bucket-owner   # low-level -> 1

$ aws s3 cp   /tmp/x s3://b/k --expected-bucket-owner 123456789012
aws: [ERROR]: An error occurred (ParamValidation): Unknown options: --expected-bucket-owner,123456789012
$ aws s3 sync /tmp   s3://b/  --expected-bucket-owner 123456789012
aws: [ERROR]: An error occurred (ParamValidation): Unknown options: --expected-bucket-owner,123456789012
```

**Conclusion:** the high-level `aws s3 cp` / `aws s3 sync` commands do **NOT**
accept `--expected-bucket-owner` — not even in the latest v2. Appending the flag
to those commands would make **every** access (legitimate or not) fail with a
`ParamValidation` error, which would *break all preservation* (Property 2) — the
opposite of the intended fix. The flag is only accepted by the low-level
`aws s3api` verbs (`head-bucket`, `get-object`, `put-object`, `list-objects-v2`,
…).

Therefore the **PRIMARY mechanism for every executable call site (B1, B2, B3)**
is an **`aws s3api head-bucket --bucket <name> --expected-bucket-owner <acct>`
preflight** emitted immediately before the corresponding download / upload
group. `head-bucket` returns HTTP `200` when the bucket is owned by `<acct>` and
`403 Access Denied` (non-zero exit) when it is not, so the preflight **fails
closed** on an owner mismatch and is a **no-op on the happy path**. This choice
also makes B1's preservation *stronger*: because we do not touch the existing
`aws s3 cp` / `aws s3 sync` strings at all, they remain byte-for-byte identical
to the sibling secrets baseline; the fix only **adds** preflight entries.

Documented **secondary defense (read-then-install paths, B1 / B4)**: a
post-download `sha256sum -c` verification against a committed manifest MAY be
layered on top for artifacts that are subsequently `dpkg -i`/`pip install`ed.
This design specifies the head-bucket preflight as the required mechanism and
records the checksum step as an optional hardening, not a requirement, to keep
the fix minimal and preservation-safe.

Per-call-site mechanism selection:

| Site | Access | Mechanism chosen | Why |
|------|--------|------------------|-----|
| B1 `deploy.py` | SSM `aws s3 sync`/`cp` (read → install) | `aws s3api head-bucket --expected-bucket-owner` preflight entries prepended to each download group | High-level cp/sync reject the flag; preflight keeps the existing command strings byte-for-byte identical to the secrets baseline and fails closed |
| B2 `publish.sh` | `aws s3 cp` `.deb`/`.whl` (write) | `aws s3api head-bucket --expected-bucket-owner "$EXPECTED_BUCKET_OWNER"` before the uploads, with `|| exit 1` | Same CLI limitation; fail-closed before publishing build outputs |
| B3 `publish.sh` | `aws s3 sync ./sphinx` (write) | `aws s3api head-bucket ...` inside the `if [ -d "./sphinx" ]` guard | Same |
| B4 `index.rst` | documented `aws s3 cp` (read → install) | `.. note::` + documented `aws s3api head-bucket --expected-bucket-owner <PANORAMA_SDK_ACCOUNT>` preflight shown *before* the `cp` block | Doc must teach a command that actually works; showing `aws s3 cp … --expected-bucket-owner` would teach a broken invocation |
| B5 `s3.rst` | config example | placeholder `"<your-bucket-name>"` + `.. note::` | Doc-only; no owner assertion applicable to a config value |
| B6 notebook | `old_prefix` literal (read) | single-source `sample_data_bucket` variable + prerequisite markdown cell | Notebook-only; parameterize + document |

**Deviation note:** the task brief proposed appending
`--expected-bucket-owner {shlex.quote(...)}` directly to the high-level
`aws s3 cp`/`aws s3 sync` commands as the PRIMARY approach. The concrete
verification above shows that would break the commands, so this design adopts the
brief's documented FALLBACK (`aws s3api head-bucket --expected-bucket-owner`
preflight) as the PRIMARY mechanism for every executable call site. The
`shlex.quote` pattern from the sibling secrets spec is preserved — it is applied
to the interpolated owner-account value in the new preflight f-strings.

### The findings

| # | File | Access | Fix |
|---|------|--------|-----|
| B1 | `src/edgemlsdk/src/test/longevity/deploy.py` | read → `dpkg -i`/`pip install` | head-bucket preflight for `panorama-sdk-v2-artifacts` + `edgeml-sdk-longevity-tests`, owner values from args/env, `shlex.quote`'d |
| B2 | `src/edgemlsdk/src/utilities/publish.sh` (~23) | write `.deb`/`.whl` | `ARTIFACT_BUCKET` env var + `EXPECTED_BUCKET_OWNER` head-bucket preflight |
| B3 | `src/edgemlsdk/src/utilities/publish.sh` (~31) | write docs | `DOCS_BUCKET` env var + head-bucket preflight |
| B4 | `src/edgemlsdk/src/docs/source/index.rst` (~42) | documented read | `.. note::` + documented head-bucket preflight |
| B5 | `src/edgemlsdk/src/docs/source/components/message_broker/s3.rst` (~87) | config example | `"<your-bucket-name>"` placeholder + `.. note::` |
| B6 | `DDA_SageMaker_Model_Training_and_Compilation.ipynb` (~213) | read prefix | `sample_data_bucket` variable + prerequisite cell |
| B7 | `test/backend-test/security/s3_squat_audit.py` (new) | — | runnable audit gate over the six in-scope files, wired into `build-custom.sh` |

### Ordering (least blast radius first)

Docs / notebook (B4, B5, B6) → publish.sh (B2, B3) → deploy.py (B1). See
Fix Implementation → Ordering and risk.

### Out of scope

The vendored / generated duplicate SDK subtree `src/backend/edgemlsdk/edgemlsdk/…`
(confirmed to contain its own `deploy.py`, `publish.sh`, `index.rst`, `s3.rst`,
and C++/Python message-broker samples referencing the same bucket names) — it
regenerates from the maintained source at `src/edgemlsdk/src/…`; only the
maintained paths B1–B6 are fixed. Also out of scope: `cdk.out/**` build
artifacts, renaming the AWS-managed buckets, and every finding from the sibling
remediation batches (injection #1–#8, secrets S1–S9, IAM I1–I17). In particular
the `# nosec B105` secret-name line and the `shlex.quote`'d SSM construction in
`deploy.py` (introduced by the secrets spec) MUST be preserved byte-for-byte.

## Glossary

- **Bug_Condition (C)**: An S3 access (an `aws s3 cp`/`aws s3 sync` call, a
  message-broker config `bucket` value, or a notebook download prefix) in an
  in-scope file (B1–B6) that targets a hardcoded, predictable bucket literal and
  is a read-then-install / build-output-write / copy-pasteable example, with NO
  adjacent owner assertion (`head-bucket --expected-bucket-owner`), integrity
  check, env-var parameterization (team-owned writes), or placeholder / ownership
  note (docs / notebook).
- **Property (P) / Fix Checking**: After the fix, every in-scope executable S3
  access is preceded by (or wrapped in) an `aws s3api head-bucket
  --expected-bucket-owner <ACCOUNT>` preflight that fails closed on owner
  mismatch; team-owned write buckets read from an env var defaulting to the
  current value; and doc / notebook references are placeholders or carry an
  ownership-verification note.
- **Preservation**: For every input that does NOT trigger the bug condition
  (every legitimate flow against a correctly-owned bucket), the fixed code
  behaves identically to the original — `F(X) = F'(X)`. `head-bucket` against a
  correctly-owned bucket returns `200` (no-op); an unset env var resolves to the
  current bucket value; doc prose / notebook logic are otherwise unchanged.
- **F / F'**: the original (unfixed) code/doc/notebook where the S3 access uses a
  hardcoded predictable bucket with no owner assertion / parameterization /
  placeholder; and the fixed code/doc/notebook where every in-scope access is
  owner-asserted, team-owned writes are parameterized, and doc/notebook
  references are placeholders or owner-noted.
- **`aws s3api head-bucket --bucket <name> --expected-bucket-owner <acct>`**: the
  low-level preflight this design uses. Returns `200`/exit-0 when `<name>` is
  owned by `<acct>`, `403`/non-zero when it is not. Chosen because the
  high-level `aws s3 cp`/`aws s3 sync` reject `--expected-bucket-owner` (verified
  above). No-op on the happy path → preservation-safe.
- **AWS-managed bucket**: a bucket owned by an AWS service team, not DDA —
  `panorama-sdk-v2-artifacts` (Panorama SDK distribution, read path) and
  `lookoutvision-us-east-1-0e205be246` (Lookout-for-Vision samples). Cannot be
  renamed; mitigated by owner assertion / verification note.
- **Team-owned bucket**: a bucket owned by the DDA / edgemlsdk team account —
  `edgeml-sdk-longevity-tests`, `edgeml-sdk-docs`, and `panorama-sdk-v2-artifacts`
  on the write path (published to by `publish.sh`). Parameterized via env var +
  owner assertion.
- **`ARTIFACTS_BUCKET_OWNER` / `LONGEVITY_BUCKET_OWNER`** (B1): the expected owner
  account IDs for the two deploy.py buckets. See B1 for sourcing.
- **`ARTIFACT_BUCKET` / `DOCS_BUCKET` / `EXPECTED_BUCKET_OWNER`** (B2/B3): the
  parameterization env vars for `publish.sh`.
- **`sample_data_bucket`** (B6): the single-source notebook variable that
  replaces the scattered `lookoutvision-*` literal.
- **In-scope files**: the six real source paths this spec owns —
  `src/edgemlsdk/src/test/longevity/deploy.py` (B1),
  `src/edgemlsdk/src/utilities/publish.sh` (B2, B3),
  `src/edgemlsdk/src/docs/source/index.rst` (B4),
  `src/edgemlsdk/src/docs/source/components/message_broker/s3.rst` (B5),
  `DDA_SageMaker_Model_Training_and_Compilation.ipynb` (B6). The audit (B7) is
  scoped strictly to these; the vendored `src/backend/edgemlsdk/edgemlsdk/…`
  duplicate and `cdk.out` are excluded.

## Bug Details

### Bug Condition

The bug manifests on any S3 access in an in-scope file that targets a hardcoded,
predictable bucket name without an owner assertion / integrity check /
parameterization / placeholder. The six sites are: the
`download_edgemlsdk_release_artifacts` SSM command list in `deploy.py`
(`aws s3 sync s3://panorama-sdk-v2-artifacts/release/…` and three
`aws s3 cp/sync s3://edgeml-sdk-longevity-tests/…` entries, then `dpkg -i` /
`pip install`) (B1); the four `aws s3 cp *.deb/*.whl
s3://panorama-sdk-v2-artifacts/release/…` uploads in `publish.sh:~23` (B2); the
`aws s3 sync ./sphinx s3://edgeml-sdk-docs/edgeml-sdk/v1/…` upload in
`publish.sh:~31` (B3); the four dependency `aws s3 cp
s3://panorama-sdk-v2-artifacts/dependencies/…` plus the `PanoramaSDK.deb` /
`.whl` release-download `code-block`s in `index.rst:~42` (B4); the
`"bucket": "panorama-sdk-v2-artifacts"` message-broker config sample in
`s3.rst:~87` (B5); and the `old_prefix =
's3://lookoutvision-us-east-1-0e205be246/getting-started/'` notebook cell (B6).

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X of type S3Access   // an aws s3 cp/sync call, a message-broker config
                              // entry, or a notebook download prefix in an
                              // in-scope file (B1-B6)
  OUTPUT: boolean

  RETURN predictableBucketLiteral(X)      // panorama-sdk-v2-artifacts,
                                          // edgeml-sdk-docs,
                                          // edgeml-sdk-longevity-tests,
                                          // lookoutvision-*
      AND (readThenInstall(X)             // download then dpkg -i / pip install
           OR buildOutputWrite(X)         // upload freshly built .deb/.whl/docs
           OR copyPasteableExample(X))    // doc / notebook reference
      AND NOT (ownerAsserted(X)           // adjacent head-bucket
                                          // --expected-bucket-owner preflight
               OR integrityVerified(X)    // post-download checksum
               OR parameterizedWithOwnerAssertion(X)  // env var + preflight
               OR placeholderOrOwnershipNoted(X))     // <...> / .. note::
END FUNCTION
```

**Expected behavior for buggy inputs (Fix Checking):**
```
FOR ALL X WHERE isBugCondition(X) DO
  result := F'(X)
  ASSERT ownerAssertedOrIntegrityVerified(result)
     // Executable read/write sites (B1, B2, B3) are preceded by an
     // aws s3api head-bucket --bucket <name> --expected-bucket-owner <acct>
     // preflight that fails closed (403) on owner mismatch. (High-level
     // aws s3 cp/sync reject --expected-bucket-owner; head-bucket is used.)
  ASSERT parameterizedWhereTeamOwned(result)
     // Team-owned write buckets are read from an env var (ARTIFACT_BUCKET,
     // DOCS_BUCKET) defaulting to the current literal.
  ASSERT placeholderOrOwnerNoted(result) WHEN X is a doc/notebook reference
     // B4: ownership/verification .. note:: + documented head-bucket preflight.
     // B5: config bucket value is <your-bucket-name> + prerequisite .. note::.
     // B6: notebook prefix is a sample_data_bucket variable preceded by a
     //     prerequisite markdown cell.
END FOR
```

### Examples

Bug manifestation on unfixed code:

- **B1** — `deploy.py`'s SSM list contains
  `f"aws s3 sync s3://panorama-sdk-v2-artifacts/release/1.0.{q_release_date}/{q_platform}/{q_ubuntu_version}/3.8.0/ /edgemlsdk"`,
  `"aws s3 cp s3://edgeml-sdk-longevity-tests/longevity.json /edgemlsdk/"`,
  `"aws s3 cp s3://edgeml-sdk-longevity-tests/delegates.json /edgemlsdk/"`, and
  `f"aws s3 sync s3://edgeml-sdk-longevity-tests/{source_folder} /edgemlsdk/{source_folder}"`,
  followed downstream by `dpkg -i Panorama_1.0.{q_release_date}.deb` and
  `pip install panorama-1.0-py3-none-any.whl`. No owner assertion precedes the
  downloads. **Expected after fix:** a
  `f"aws s3api head-bucket --bucket panorama-sdk-v2-artifacts --expected-bucket-owner {shlex.quote(str(artifacts_bucket_owner))}"`
  entry precedes the `panorama-sdk-v2-artifacts` sync, and a
  `f"aws s3api head-bucket --bucket edgeml-sdk-longevity-tests --expected-bucket-owner {shlex.quote(str(longevity_bucket_owner))}"`
  entry precedes the three `edgeml-sdk-longevity-tests` accesses; the existing
  `cp`/`sync` strings are byte-for-byte unchanged.
- **B2** — `publish.sh` runs
  `aws s3 cp *.deb s3://panorama-sdk-v2-artifacts/release/$version/…` (+ `latest`
  and the `.whl` pair) with no env var and no owner assertion. **Expected after
  fix:** `ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-panorama-sdk-v2-artifacts}"`, a
  head-bucket preflight, then the same uploads against `s3://${ARTIFACT_BUCKET}/…`.
- **B3** — `aws s3 sync ./sphinx s3://edgeml-sdk-docs/edgeml-sdk/v1/$major_minor/`
  with no env var / assertion. **Expected after fix:**
  `DOCS_BUCKET="${DOCS_BUCKET:-edgeml-sdk-docs}"`, a head-bucket preflight inside
  the `if [ -d "./sphinx" ]` guard, then the sync against `s3://${DOCS_BUCKET}/…`.
- **B4** — `index.rst` documents `aws s3 cp
  s3://panorama-sdk-v2-artifacts/dependencies/…/aws-c-iot.deb ./` then
  `dpkg -i aws-c-iot.deb` (× 4 deps + the `PanoramaSDK.deb`/`.whl` releases) with
  no note. **Expected after fix:** a `.. note::` about verifying the AWS-managed
  distribution bucket, and a documented `aws s3api head-bucket
  --bucket panorama-sdk-v2-artifacts --expected-bucket-owner <PANORAMA_SDK_ACCOUNT>`
  preflight shown before the `cp` block. Prose and `dpkg`/`pip` steps preserved.
- **B5** — `s3.rst`'s config sample has `"bucket": "panorama-sdk-v2-artifacts"`.
  **Expected after fix:** `"bucket": "<your-bucket-name>"` + a prerequisite
  `.. note::`. Rest of the JSON structure preserved.
- **B6** — the notebook cell sets
  `old_prefix = 's3://lookoutvision-us-east-1-0e205be246/getting-started/'`.
  **Expected after fix:** a `sample_data_bucket =
  "lookoutvision-us-east-1-0e205be246"  # AWS-owned L4V sample bucket; replace
  with your own` variable and `old_prefix = f's3://{sample_data_bucket}/getting-started/'`,
  preceded by a prerequisite markdown cell. `update_manifest_paths` unchanged.

Edge cases (preserved, NOT buggy):

- The `deploy.py` `upload_folder_to_s3` / `upload_file_to_s3` calls use the boto3
  `s3_client` (not `aws s3 cp/sync`) against `edgeml-sdk-longevity-tests` — these
  are out of the finding's `aws s3 cp/sync` scope, but the head-bucket preflight
  the fix adds sits at the top of the SSM download list on the deployed instance;
  the boto3 uploads on the deployer host are unchanged.
- The `deploy.py` `# nosec B105` bucket-name line and the `shlex.quote`'d
  `q_region`/`q_platform`/… interpolation (secrets-spec baseline) are unchanged.
- The `ecr get-login-password` / `docker pull` SSM entries in B1's list are not
  S3 accesses — unchanged.
- `index.rst`'s `apt-get` / `pip install Cython numpy boto3` prerequisite
  `code-block`s (no S3) — unchanged.
- `s3.rst`'s `"region"` / `"key"` / `"overwrite"` config keys, the C++/Python
  `literalinclude` samples, and the API docs — unchanged.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- The longevity deploy still downloads SDK release artifacts and test configs
  from correctly-owned `panorama-sdk-v2-artifacts` and
  `edgeml-sdk-longevity-tests` and `dpkg -i` / `pip install`s them — the
  head-bucket preflight is a no-op (`200`) when the bucket is owned by the
  expected account. The existing `aws s3 cp`/`aws s3 sync` SSM command strings
  remain byte-for-byte identical to the sibling secrets baseline (only preflight
  entries are added).
- `deploy.py`'s `# nosec B105` line, the `shlex.quote`'d argument interpolation,
  the boto3 upload helpers, the `ecr`/`docker` SSM entries, and the `mqtt`
  branch are byte-for-byte identical.
- `publish.sh` run without `ARTIFACT_BUCKET` / `DOCS_BUCKET` set resolves them to
  `panorama-sdk-v2-artifacts` / `edgeml-sdk-docs` (the current literals) and,
  against correctly-owned buckets, uploads the same versioned **and** `latest`
  `.deb`/`.whl` and syncs docs to the same `edgeml-sdk/v1/$major_minor/` path.
  The `if [ -d "./sphinx" ]` guard and the release-path layout are preserved.
- `index.rst`'s installation prose, the four `dpkg -i` steps, the `pip install`
  step, the `code-block` captions, and the toctree are unchanged; only a
  `.. note::` and a documented preflight line are added.
- `s3.rst`'s target-parameter descriptions, the `"region"`/`"key"`/`"overwrite"`
  keys, and every other section are unchanged; only the `bucket` value becomes a
  placeholder and a `.. note::` is added.
- The notebook's `update_manifest_paths` logic, the `wget` of the GitHub
  manifest, the upload/cleanup steps, and every other cell are unchanged; only
  the `old_prefix` derivation changes and a prerequisite markdown cell is added.
  The notebook JSON remains valid.

**Scope:**
All inputs that do NOT trigger the bug condition must be completely unaffected.
This explicitly includes:
- Every legitimate download / upload / example against a bucket owned by the
  expected account (`head-bucket` returns `200`, so behavior is identical).
- The entire vendored `src/backend/edgemlsdk/edgemlsdk/…` duplicate subtree
  (its `deploy.py`, `publish.sh`, `index.rst`, `s3.rst`, and message-broker
  samples), which regenerates from the maintained source.
- `cdk.out/**` build artifacts.
- Every finding from the sibling remediation batches (#1–#8, S1–S9, I1–I17),
  already remediated on separate branches.

**Note:** the expected correct behavior for buggy inputs is defined in the
Correctness Properties section (Property 1); this section focuses on what must
NOT change.

## Hypothesized Root Cause

The deploy tooling, publish script, docs, and notebook were written for an
internal single-account context where the bucket names were well-known team /
AWS assets and "it just works" against them, so the accesses were hardcoded with
no owner assertion. Concretely:

1. **Predictable literals baked into command strings (B1, B2, B3).** The SSM
   command list and the publish uploads embed `panorama-sdk-v2-artifacts`,
   `edgeml-sdk-longevity-tests`, and `edgeml-sdk-docs` directly. There was no
   notion that a deleted-and-re-squatted bucket could be served by a different
   account, and the AWS CLI does not assert bucket ownership by default.
2. **The `--expected-bucket-owner` gap on high-level commands.** Even a
   security-conscious author who tried to add `--expected-bucket-owner` to
   `aws s3 cp`/`aws s3 sync` would find the flag rejected (verified above), so
   the natural but wrong instinct is to give up on owner assertion. The correct
   mechanism (a low-level `aws s3api head-bucket --expected-bucket-owner`
   preflight) is non-obvious.
3. **Copy-pasteable docs and notebooks (B4, B5, B6).** The install docs, the
   message-broker config sample, and the training notebook were written to be
   short and directly runnable, so they embed real bucket names that users copy
   verbatim — propagating the squatting exposure into customer environments.
4. **No regression gate (B7).** Nothing asserts the fixes stay in place, so a
   future edit could reintroduce a hardcoded predictable-bucket access with no
   owner assertion.

## Correctness Properties

Property 1: Bug Condition — Every in-scope S3 access is squatting-resistant

_For any_ S3 access where the bug condition holds (`isBugCondition` returns true
— a predictable bucket literal read-then-installed / build-output-written /
shown in a copy-pasteable example in an in-scope file with no owner assertion /
integrity check / parameterization / placeholder), the fixed code SHALL make the
access squatting-resistant:

- **Executable read/write sites (B1, B2, B3)** SHALL be preceded by an
  `aws s3api head-bucket --bucket <name> --expected-bucket-owner <ACCOUNT>`
  preflight that fails closed (`403`, non-zero exit) when the bucket is owned by
  an account other than `<ACCOUNT>`, so a squatted bucket cannot serve or receive
  artifacts. (The high-level `aws s3 cp`/`aws s3 sync` commands reject
  `--expected-bucket-owner`, verified against AWS CLI v2.35.19; the low-level
  `head-bucket` preflight is the mechanism.) For B1, the owner values are sourced
  from args/env (`--artifacts-bucket-owner`/`ARTIFACTS_BUCKET_OWNER` for the
  AWS-managed Panorama bucket; `--longevity-bucket-owner`/`LONGEVITY_BUCKET_OWNER`
  defaulting to the deployer's `sts get-caller-identity` account for the
  team-owned longevity bucket), `shlex.quote`'d into the preflight f-strings, and
  the existing `aws s3 cp`/`aws s3 sync` command strings remain byte-for-byte
  identical to the sibling secrets baseline.
- **Team-owned write buckets (B2 `panorama-sdk-v2-artifacts`, B3
  `edgeml-sdk-docs`)** SHALL be read from an env var (`ARTIFACT_BUCKET`,
  `DOCS_BUCKET`) defaulting to the current literal, with `EXPECTED_BUCKET_OWNER`
  defaulting to `aws sts get-caller-identity --query Account --output text`.
- **Doc / notebook references** SHALL be placeholder or owner-noted: B4 shows an
  ownership-verification `.. note::` and a documented `aws s3api head-bucket
  --expected-bucket-owner <PANORAMA_SDK_ACCOUNT>` preflight before the `cp`
  block; B5 shows `"bucket": "<your-bucket-name>"` and a prerequisite `.. note::`;
  B6 derives the prefix from a single-source `sample_data_bucket` variable
  preceded by a prerequisite markdown cell.

A full-repo audit over the six in-scope files finds no remaining disallowed
occurrence — no predictable bucket literal on an `aws s3 cp`/`aws s3 sync` line
or config/notebook download without an adjacent `--expected-bucket-owner` /
`head-bucket` preflight / placeholder / ownership note / `# nosec` — other than
occurrences carrying a documented, justified exception.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7**

Property 2: Preservation — No behavior change for legitimate flows

_For any_ S3 access where the bug condition does NOT hold (`isBugCondition`
returns false — every legitimate download / upload / example against a bucket
owned by the expected account), the fixed code SHALL produce the same result as
the original code (`F(X) = F'(X)`), preserving: the longevity deploy's successful
download-and-install from correctly-owned `panorama-sdk-v2-artifacts` and
`edgeml-sdk-longevity-tests` (the `head-bucket` preflight is a no-op `200` when
the bucket is correctly owned); the `deploy.py` SSM command strings byte-for-byte
identical to the secrets baseline except for the added preflight entries, with
the `shlex.quote`'d args and `# nosec B105` line preserved; `publish.sh`'s
resolution of `ARTIFACT_BUCKET`/`DOCS_BUCKET` to the current literals when the
env vars are unset and its versioned+`latest` dual-upload / `edgeml-sdk/v1/…`
docs-sync layout and `if [ -d "./sphinx" ]` guard; `index.rst`'s installation
prose, `dpkg`/`pip` steps, and `code-block` captions; `s3.rst`'s config
structure and every non-`bucket` key; the notebook's `update_manifest_paths`
logic, download/upload/cleanup steps, and valid JSON; and every file / line in
the in-scope files that is NOT one of B1–B6, plus the entire vendored
`src/backend/edgemlsdk/edgemlsdk/…` duplicate.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8**

## Fix Implementation

### Changes Required

Assuming the root-cause analysis is correct, each site gets the minimal change
that makes `isBugCondition` false for it while preserving `F(X) = F'(X)`.

#### B1 — `src/edgemlsdk/src/test/longevity/deploy.py` (Req 2.1) — head-bucket preflight in the SSM list

**Function**: `main(args)`, the `download_edgemlsdk_release_artifacts` list.

1. **Source the expected owner account IDs** near the top of `main`, alongside
   the existing `session`/`aws_region` setup:
   ```python
   # Expected S3 bucket owners for the squatting preflight (Group 5 / B1).
   # panorama-sdk-v2-artifacts is the AWS-managed Panorama SDK distribution
   # bucket -> its owner account differs from the deployer, so it MUST be
   # supplied explicitly (arg/env), defaulting to the documented Panorama SDK
   # distribution account constant.
   artifacts_bucket_owner = (
       args.artifacts_bucket_owner
       or os.environ.get("ARTIFACTS_BUCKET_OWNER")
       or PANORAMA_SDK_DISTRIBUTION_ACCOUNT   # documented module constant, filled at impl time
   )
   # edgeml-sdk-longevity-tests is team-owned by the deployer's account, so it
   # defaults to the caller identity (a no-op preflight for the legitimate
   # deployer).
   sts_client = session.client("sts", region_name=aws_region)
   longevity_bucket_owner = (
       args.longevity_bucket_owner
       or os.environ.get("LONGEVITY_BUCKET_OWNER")
       or sts_client.get_caller_identity()["Account"]
   )
   ```
2. **Add two argparse args** (mirroring the existing `--platform`/`--region`
   pattern): `--artifacts-bucket-owner` and `--longevity-bucket-owner`, both
   `type=str, default=None`, so the resolution above falls through to env /
   caller-identity / documented-constant.
3. **Prepend `head-bucket` preflight entries** into the
   `download_edgemlsdk_release_artifacts` list, immediately before the
   corresponding `aws s3` entries, using the same `shlex.quote` discipline the
   secrets spec introduced (the bucket names stay bare literals exactly as the
   `s3://…` literals already are; only the interpolated owner value is quoted):
   ```python
   q_artifacts_owner = shlex.quote(str(artifacts_bucket_owner))
   q_longevity_owner = shlex.quote(str(longevity_bucket_owner))
   download_edgemlsdk_release_artifacts = [
       "sudo yum update",
       "sudo yum install docker -y",
       "sudo service docker start",
       "sudo service docker status",
       f"export AWS_DEFAULT_REGION={q_region}",
       "sudo mkdir -p /edgemlsdk",
       f"sudo mkdir -p /edgemlsdk/{source_folder}",
       # --- B1: fail closed if panorama-sdk-v2-artifacts is not owned by the
       #     expected AWS account (squatting preflight). ---
       f"aws s3api head-bucket --bucket panorama-sdk-v2-artifacts --expected-bucket-owner {q_artifacts_owner}",
       f"aws s3 sync s3://panorama-sdk-v2-artifacts/release/1.0.{q_release_date}/{q_platform}/{q_ubuntu_version}/3.8.0/ /edgemlsdk",
       # --- B1: fail closed if edgeml-sdk-longevity-tests is not owned by the
       #     expected account. ---
       f"aws s3api head-bucket --bucket edgeml-sdk-longevity-tests --expected-bucket-owner {q_longevity_owner}",
       "aws s3 cp s3://edgeml-sdk-longevity-tests/longevity.json /edgemlsdk/",
       "aws s3 cp s3://edgeml-sdk-longevity-tests/delegates.json /edgemlsdk/",
       f"aws s3 sync s3://edgeml-sdk-longevity-tests/{source_folder} /edgemlsdk/{source_folder}",
       "aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.us-west-2.amazonaws.com",
       f"docker pull ACCOUNT_ID.dkr.ecr.us-west-2.amazonaws.com/edgemlsdk:{q_ubuntu_version}-{q_platform}-{q_python_version}-latest",
   ]
   ```
   **CRITICAL:** every pre-existing entry is byte-for-byte identical to the
   secrets baseline — the four `aws s3 cp`/`aws s3 sync` strings, the `export`,
   the `mkdir`s, the `ecr`/`docker` lines. The fix ADDS only the two
   `aws s3api head-bucket …` entries. Because `AWS-RunShellScript` runs the list
   sequentially and a non-zero exit aborts the command batch, a `403` from
   `head-bucket` fails the deploy closed before any `dpkg -i`/`pip install`.
4. **`shlex` and `os` are already imported** (the secrets spec added `shlex`;
   `os` is used by the upload helpers) — no new imports beyond `sts` client
   creation.
5. **Preserve** the `mqtt` branch, the boto3 upload helpers, and the
   `# nosec B105` line byte-for-byte.

**Optional secondary hardening (not required):** a post-download
`sha256sum -c panorama.sha256` entry against a committed manifest, added after
the `panorama-sdk-v2-artifacts` sync, for defense-in-depth on the
read-then-install path. Recorded here; not part of the required B1 change.

#### B2 — `src/edgemlsdk/src/utilities/publish.sh` (Req 2.2) — parameterize artifact bucket + owner preflight

**Location**: the top of the script and the four `.deb`/`.whl` upload lines
(~23–27).

1. **Introduce env-var parameterization** near the top (after the existing
   `version`/`python_version`/`ubuntu_version` assignments):
   ```sh
   ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-panorama-sdk-v2-artifacts}"
   DOCS_BUCKET="${DOCS_BUCKET:-edgeml-sdk-docs}"
   # Expected owner of the distribution buckets; defaults to the publishing
   # account. Set an account-scoped value in CI to fail closed on a squatted
   # bucket. (aws s3 cp/sync do not accept --expected-bucket-owner, so a
   # head-bucket preflight is used.)
   EXPECTED_BUCKET_OWNER="${EXPECTED_BUCKET_OWNER:-$(aws sts get-caller-identity --query Account --output text)}"
   ```
2. **Add a fail-closed head-bucket preflight** before the uploads:
   ```sh
   aws s3api head-bucket --bucket "$ARTIFACT_BUCKET" \
     --expected-bucket-owner "$EXPECTED_BUCKET_OWNER" \
     || { echo "ERROR: $ARTIFACT_BUCKET is not owned by $EXPECTED_BUCKET_OWNER (possible bucket squatting); aborting publish." >&2; exit 1; }
   ```
   (Explicit `|| { … exit 1; }` because the script has no `set -e`; this keeps
   the happy path unchanged and fails closed on mismatch.)
3. **Rewrite the four upload lines** to use `s3://${ARTIFACT_BUCKET}/…`,
   preserving the versioned + `latest` dual-upload and the exact release-path
   layout:
   ```sh
   aws s3 cp *.deb s3://${ARTIFACT_BUCKET}/release/$version/$(uname -m)/$ubuntu_version/$python_version/
   aws s3 cp *.deb s3://${ARTIFACT_BUCKET}/release/latest/$(uname -m)/$ubuntu_version/$python_version/PanoramaSDK.deb
   aws s3 cp ./lib/python_package/dist/*.whl s3://${ARTIFACT_BUCKET}/release/$version/$(uname -m)/$ubuntu_version/$python_version/
   aws s3 cp ./lib/python_package/dist/*.whl s3://${ARTIFACT_BUCKET}/release/latest/$(uname -m)/$ubuntu_version/$python_version/
   ```

#### B3 — `src/edgemlsdk/src/utilities/publish.sh` (Req 2.3) — parameterize docs bucket + owner preflight

**Location**: the `if [ -d "./sphinx" ]` block (~30–32).

1. **Rewrite the docs sync** inside the existing guard, adding a head-bucket
   preflight and using `s3://${DOCS_BUCKET}/…`, preserving the
   `edgeml-sdk/v1/$major_minor/` layout:
   ```sh
   if [ -d "./sphinx" ]; then
       major_minor=$(echo "$version" | cut -d'.' -f1,2)
       aws s3api head-bucket --bucket "$DOCS_BUCKET" \
         --expected-bucket-owner "$EXPECTED_BUCKET_OWNER" \
         || { echo "ERROR: $DOCS_BUCKET is not owned by $EXPECTED_BUCKET_OWNER (possible bucket squatting); aborting docs upload." >&2; exit 1; }
       aws s3 sync ./sphinx s3://${DOCS_BUCKET}/edgeml-sdk/v1/$major_minor/
   fi
   ```
2. **Preserve** the `if [ -d "./sphinx" ]` guard and the `major_minor`
   derivation.

#### B4 — `src/edgemlsdk/src/docs/source/index.rst` (Req 2.4) — verification note + documented preflight (doc-only)

**Location**: before the dependency `code-block` (~40) and the release-download
`code-block`s (~57–65).

1. **Add a `.. note::`** immediately before the dependency `aws s3 cp`
   `code-block` (and referenced again before the release-download blocks):
   ```rst
   .. note::

      These commands download pre-built artifacts from the AWS-managed Panorama
      SDK distribution bucket ``panorama-sdk-v2-artifacts``. Before installing,
      verify the bucket is owned by the expected AWS account so a squatted
      same-named bucket cannot serve a trojaned package. The ``aws s3 cp``
      high-level command does not accept ``--expected-bucket-owner``; run the
      ``aws s3api head-bucket`` preflight shown below (replace
      ``<PANORAMA_SDK_ACCOUNT>`` with the documented Panorama SDK distribution
      account ID).
   ```
2. **Prepend a documented head-bucket preflight** line inside the dependency
   `code-block` (before the four `aws s3 cp … .deb ./` lines):
   ```bash
   aws s3api head-bucket --bucket panorama-sdk-v2-artifacts --expected-bucket-owner <PANORAMA_SDK_ACCOUNT>
   ```
   and the same preflight line at the top of the `Debian Package` and
   `Python Wheel` release-download `code-block`s.
3. **Preserve** all prose, the four `dpkg -i` steps, the `pip install` step, the
   `:caption:` directives, and the toctree byte-for-byte. Deviation from the task
   brief: the brief said to add `--expected-bucket-owner` to the `aws s3 cp`
   commands; because that flag is rejected by `aws s3 cp` (verified), the doc
   instead shows the `aws s3api head-bucket` preflight so users are taught a
   command that works.

#### B5 — `src/edgemlsdk/src/docs/source/components/message_broker/s3.rst` (Req 2.5) — placeholder + prerequisite note (doc-only)

**Location**: the `Message Broker Config Sample` JSON `code-block` (~87).

1. **Change** `"bucket": "panorama-sdk-v2-artifacts"` to
   `"bucket": "<your-bucket-name>"`.
2. **Add a `.. note::`** before the sample:
   ```rst
   .. note::

      Replace ``<your-bucket-name>`` with a bucket you own. Do not publish to a
      bucket you do not control; a predictable, unowned bucket name can be
      squatted by another account.
   ```
3. **Preserve** every other key in the sample (`region`, `key`, `overwrite`,
   `batch_payload_expansion`), the target-parameter descriptions, the
   `literalinclude` samples, and the API section byte-for-byte.

#### B6 — `DDA_SageMaker_Model_Training_and_Compilation.ipynb` (Req 2.6) — single-source variable + prerequisite cell (notebook-only)

**Location**: the `seg_manifest` code cell (`old_prefix` at source line ~213).

1. **Insert a new markdown cell** immediately before the `seg_manifest` cell:
   ```markdown
   ### Prerequisite: sample data bucket ownership

   The segmentation manifest below references the AWS-owned Lookout for Vision
   sample bucket `lookoutvision-us-east-1-0e205be246`. Before running this cell,
   create your own bucket for your data and/or verify that the AWS-owned sample
   bucket is the expected one — a predictable bucket name in a region where the
   real bucket does not exist could be squatted by another account. Set
   `sample_data_bucket` below to the bucket you intend to read the sample data
   from.
   ```
2. **Replace the hardcoded prefix** in the `seg_manifest` cell `source` array
   with a single-source variable so the literal appears exactly once, documented:
   ```python
   # AWS-owned L4V sample bucket; replace with your own after verifying ownership.
   sample_data_bucket = "lookoutvision-us-east-1-0e205be246"
   old_prefix = f's3://{sample_data_bucket}/getting-started/'
   ```
3. **Preserve** the `update_manifest_paths` function, the `wget` of the GitHub
   manifest, the `segmentation_lines = update_manifest_paths(...)` call, the
   save / upload / cleanup steps, and every other cell. The new cell and the
   edited `source` array must keep the `.ipynb` JSON valid (proper `cell_type`,
   `metadata`, `outputs`, `source` list-of-strings with `\n` line endings, and
   a unique cell `id`).

#### B7 — Repo audit gate (Req 2.7) — `test/backend-test/security/s3_squat_audit.py`

Add a companion audit module mirroring the sibling `iam_audit.py` /
`secrets_audit.py` and wire it into `build-custom.sh` as a fourth gate. Details
in Testing Strategy → Repo-audit design.

### Ordering and risk (three waves)

**Wave 1 — Docs + notebook (B4, B5, B6)** — documentation / notebook only, **zero
runtime blast radius**. B4/B5 change `.rst` prose + a placeholder; B6 changes a
notebook variable + adds a markdown cell. A customer who copies the docs today
sees the safer pattern next time; nothing running changes. Preservation is "prose
/ notebook logic byte-for-byte identical apart from the note / placeholder /
variable". Land these first.

**Wave 2 — publish.sh (B2, B3)** — release tooling. Re-running `publish.sh`
uploads to the same buckets; the only behavior change is a fail-closed preflight
that is a no-op when the buckets are correctly owned and the `EXPECTED_BUCKET_OWNER`
default (caller identity) matches. Risk: if `EXPECTED_BUCKET_OWNER` is set to a
wrong account in CI, publishing aborts — recoverable by unsetting/fixing the env
var. Land after the docs wave.

**Wave 3 — deploy.py (B1)** — highest risk: it runs on a deployed EC2 instance
via SSM and gates `dpkg -i` / `pip install`. The preflight must resolve the
correct owner accounts (`artifacts_bucket_owner` for the AWS-managed Panorama
bucket in particular, since it cannot default to caller identity). Land last,
after confirming the two owner account IDs, so a mis-set owner cannot silently
break the longevity deploy. Preserves the secrets baseline exactly (existing SSM
strings unchanged; only preflight entries added).

**Highest-risk areas to watch:**
- **B1 `artifacts_bucket_owner` correctness.** `panorama-sdk-v2-artifacts` is
  AWS-managed; its owner account is NOT the deployer's, so it must come from
  arg/env or the documented `PANORAMA_SDK_DISTRIBUTION_ACCOUNT` constant. A wrong
  value fails the deploy closed. The deployment runbook must record the correct
  account ID.
- **B1 SSM ordering.** The `head-bucket` preflight entries must be positioned
  immediately before their corresponding `aws s3` entries and the batch must
  abort on a non-zero preflight exit (AWS-RunShellScript sequential semantics),
  or a squatted bucket could still be reached.
- **B2/B3 `set -e` absence.** `publish.sh` has no `set -e`; the preflight must use
  an explicit `|| { … exit 1; }` to fail closed.
- **B6 notebook JSON validity.** The inserted markdown cell and edited `source`
  array must keep the notebook parseable by `nbformat` / `json.load`.

## Testing Strategy

### Validation Approach

Two phases: first surface counterexamples that demonstrate the predictable-bucket
/ no-owner-assertion pattern on the **unfixed** tree (repo audit + targeted
inspection), then verify the fix **owner-asserts / parameterizes / placeholders**
every buggy access (Fix Checking) and **preserves** behavior for every legitimate
input (Preservation Checking, `F(X) = F'(X)`). Property-based testing (Hypothesis
— the repo already vendors `.hypothesis/`) is emphasized where the input domain
is generatable: owner-matches vs owner-mismatch accounts (preflight no-op vs
fail-closed), and env-var set vs unset (resolved bucket + owner shape).

### Repo-audit design (Req 2.7 / B7)

**Decision: add a companion `test/backend-test/security/s3_squat_audit.py`**
rather than extend the sibling `repo_audit.py`, `secrets_audit.py`, or
`iam_audit.py`. Rationale (least-duplicative option): the four gates own
different patterns and in-scope file sets; editing an existing gate would
entangle four specs' assertions and risk regressing three already-green gates. To
avoid duplication, `s3_squat_audit.py` **imports the proven low-level helpers**
from the sibling modules — `REPO_ROOT`, `EXCLUDE_DIRS`, `EXCLUDED_PATH_SUBSTRING`,
`Hit`, `_parse_line`, `_is_comment_line`, `_has_nosem` (with the same
try/except fallback re-implementation `iam_audit.py` uses when `repo_audit` is not
importable) — and defines only its own `AUDIT_PATTERNS`, `IN_SCOPE_FILES`,
`PREDICTABLE_BUCKETS`, and precise `_is_disallowed` logic. It mirrors the
siblings' two-layer shape: a raw `run_audit()` broad enumeration (non-empty on
the unfixed tree, used by the exploration test) and a precise `disallowed_hits()`
gate (zero after fix, minus documented exceptions).

**`PREDICTABLE_BUCKETS`** (the literals in scope):
```python
PREDICTABLE_BUCKETS = (
    "panorama-sdk-v2-artifacts",
    "edgeml-sdk-docs",
    "edgeml-sdk-longevity-tests",
    "lookoutvision-",            # prefix match: lookoutvision-us-east-1-0e205be246
)
```

**`IN_SCOPE_FILES`** (relative to `REPO_ROOT`) — the six real source paths this
spec owns, excluding the vendored duplicate and `cdk.out`:
- `src/edgemlsdk/src/test/longevity/deploy.py` (B1)
- `src/edgemlsdk/src/utilities/publish.sh` (B2, B3)
- `src/edgemlsdk/src/docs/source/index.rst` (B4)
- `src/edgemlsdk/src/docs/source/components/message_broker/s3.rst` (B5)
- `DDA_SageMaker_Model_Training_and_Compilation.ipynb` (B6)

**RAW enumeration patterns** (`AUDIT_PATTERNS`, line-based, deliberately broad —
`run_audit()` surfaces every token so the exploration test can list
counterexamples per finding):
```python
AUDIT_PATTERNS = [
    # a predictable bucket literal on an aws s3 cp/sync line
    ("s3_cli_predictable_bucket", r"aws\s+s3\s+(?:cp|sync)\b.*(?:panorama-sdk-v2-artifacts|edgeml-sdk-docs|edgeml-sdk-longevity-tests|lookoutvision-)"),
    # a predictable bucket literal in an s3:// URI (config / notebook / docs)
    ("s3_uri_predictable_bucket", r"s3://(?:panorama-sdk-v2-artifacts|edgeml-sdk-docs|edgeml-sdk-longevity-tests|lookoutvision-)"),
    # a predictable bucket literal as a config "bucket" value
    ("config_predictable_bucket", r"\"bucket\"\s*:\s*\"(?:panorama-sdk-v2-artifacts|edgeml-sdk-docs|edgeml-sdk-longevity-tests|lookoutvision-)"),
    # an owner assertion / preflight token (used to CLEAR a nearby access)
    ("owner_assertion", r"--expected-bucket-owner|head-bucket"),
    # a placeholder token (used to CLEAR a doc/config reference)
    ("placeholder", r"<your-bucket-name>|<PANORAMA_SDK_ACCOUNT>|sample_data_bucket"),
]
```

**Precise gate semantics** (`disallowed_hits()`; a hit is *disallowed* only when
it is in `IN_SCOPE_FILES`, is not a comment-only line, carries no
`# nosec`/`// nosec` marker, and matches the rule):

- **`unverified_s3_access`** — a line (or its logical block) containing an
  `aws s3 cp`/`aws s3 sync` against a `PREDICTABLE_BUCKETS` literal, OR an
  `s3://<predictable>` URI on a download path, where the **same logical block**
  (the line itself, the preceding 1–3 lines, or, for shell/SSM lists, the
  immediately-preceding list entry / statement) does NOT contain an
  `--expected-bucket-owner` / `head-bucket` preflight token and no `# nosec`
  marker. For `deploy.py`, the "logical block" is the SSM list: a
  `panorama-sdk-v2-artifacts` `aws s3 sync` entry is cleared iff an
  `aws s3api head-bucket --bucket panorama-sdk-v2-artifacts --expected-bucket-owner`
  entry precedes it in the list; the three `edgeml-sdk-longevity-tests` entries
  are cleared by the single `head-bucket --bucket edgeml-sdk-longevity-tests`
  entry that precedes them. After B1/B2/B3 the disallowed hits here are gone.
- **`unverified_config_reference`** — a `"bucket": "<predictable>"` config value
  (B5-shape) or a notebook download prefix bound to a bare predictable literal
  (B6-shape) where the value is not a placeholder (`<…>`) and is not derived from
  a documented single-source variable (`sample_data_bucket`) with an adjacent
  ownership note. After B5/B6 the disallowed hits here are gone.
- **`undocumented_doc_command`** — an `index.rst` `aws s3 cp` example against a
  predictable bucket in a `code-block` that has no preceding `.. note::` about
  ownership verification and no documented `head-bucket` preflight in the same
  block. After B4 the disallowed hits here are gone.

The gate parses `deploy.py`'s SSM list and `publish.sh` structurally enough to
associate a `head-bucket` preflight with the access it guards (nearest preceding
preflight for the same bucket in the same list/script), rather than merely
checking file-global presence, so dropping a preflight for one bucket while
keeping another's still fails the gate.

**Scoping precision** (mirroring the sibling gates): asserted ONLY over
`IN_SCOPE_FILES`, so it does NOT match:
- The vendored `src/backend/edgemlsdk/edgemlsdk/…` duplicate (its `deploy.py`,
  `publish.sh`, `index.rst`, `s3.rst`, and the C++/Python message-broker
  samples) — excluded by not being in `IN_SCOPE_FILES` and, defensively, via an
  `EXCLUDED_PATH_SUBSTRING`-style check for `os.path.join("edgemlsdk", "edgemlsdk")`.
- `cdk.out/**` (via the inherited `EXCLUDED_PATH_SUBSTRING`).
- The security test/fixture files' own pattern strings.
- Any other spec's files.

**Two-layer API** (matching sibling gates):
- `run_audit()` — raw broad enumeration; no scoping/exception handling; returns
  every matched hit. Non-empty on the unfixed tree (exploration test).
- `disallowed_hits()` — precise post-fix gate; applies `IN_SCOPE_FILES` scoping,
  `_has_nosem` / placeholder / preflight-association exception handling. Returns
  `[]` after the fix; non-zero exit if any element remains.

**CI wiring**: add the new gate as a **fourth** block next to the three existing
sibling gates in `build-custom.sh` (the "Security … audit gate" region, after the
IAM gate at ~line 254–258), under the same `set -e`-guarded backend-test block, so
a non-zero exit fails the build:
```sh
echo "Running security S3 bucket-squatting audit gate..."
python${PYTHON_VERSION} test/backend-test/security/s3_squat_audit.py
python${PYTHON_VERSION} -m pytest \
  test/backend-test/security/test_s3_squat_bug_condition_exploration.py -v
echo "Security S3 bucket-squatting audit gate passed."
```
(The shared `security/preservation` suite is already run by the Group-1 gate; the
S3-squatting preservation tests below live under it as `test_preservation_s3_*`.)

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate each predictable-bucket /
no-owner-assertion access BEFORE the fix and confirm/refute the root-cause
analysis. If refuted, re-hypothesize.

**Test Plan**: Run `s3_squat_audit.run_audit()` to enumerate every hit across the
six in-scope files, and add targeted tests that observe the unverified access on
unfixed code.

**Test Cases** (`test/backend-test/security/test_s3_squat_bug_condition_exploration.py`):
1. **B1 SSM downloads**: `run_audit()` returns `s3_cli_predictable_bucket` /
   `s3_uri_predictable_bucket` hits on `deploy.py` for the
   `panorama-sdk-v2-artifacts` sync and the three `edgeml-sdk-longevity-tests`
   accesses, and `disallowed_hits()` flags them as `unverified_s3_access` because
   no `head-bucket` preflight precedes them (counterexample). After the fix these
   are cleared by the added preflight entries.
2. **B2 publish uploads**: hits on `publish.sh:~23` for the four `.deb`/`.whl`
   `aws s3 cp` lines against `panorama-sdk-v2-artifacts` with no preflight — after
   fix, cleared by the `ARTIFACT_BUCKET` + `head-bucket` preflight.
3. **B3 docs sync**: hit on `publish.sh:~31` for `aws s3 sync ./sphinx
   s3://edgeml-sdk-docs/…` — after fix, cleared by the `DOCS_BUCKET` + preflight.
4. **B4 doc commands**: `undocumented_doc_command` hits on `index.rst` for the
   dependency and release `aws s3 cp` blocks with no ownership note — after fix,
   cleared by the `.. note::` + documented `head-bucket` preflight.
5. **B5 config sample**: `config_predictable_bucket` /
   `unverified_config_reference` hit on `s3.rst:~87` for
   `"bucket": "panorama-sdk-v2-artifacts"` — after fix, cleared by the
   `<your-bucket-name>` placeholder.
6. **B6 notebook prefix**: `s3_uri_predictable_bucket` /
   `unverified_config_reference` hit on the notebook `old_prefix` literal — after
   fix, cleared by the `sample_data_bucket` variable + prerequisite cell.

**Expected Counterexamples**:
- Non-empty `s3_squat_audit.run_audit()` hits across every category (B1–B6).
- Every in-scope `aws s3 cp`/`aws s3 sync` line / `s3://` URI / config `bucket`
  value targets a `PREDICTABLE_BUCKETS` literal with no adjacent
  `--expected-bucket-owner` / `head-bucket` / placeholder / note.
- Confirmation of the CLI-support root cause: `aws s3 cp --expected-bucket-owner`
  and `aws s3 sync --expected-bucket-owner` return `ParamValidation: Unknown
  options`, while `aws s3api head-bucket --expected-bucket-owner` is accepted
  (recorded in the Overview; re-assert in a test that skips gracefully when the
  AWS CLI is not installed in the runner).

### Fix Checking

**Goal**: For all inputs where the bug condition holds, the fixed code
owner-asserts / parameterizes / placeholders.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := fixedFunction(input)
  ASSERT ownerAssertedOrIntegrityVerified(result)
     AND parameterizedWhereTeamOwned(result)
     AND placeholderOrOwnerNoted(result) WHEN doc/notebook
END FOR
```

Concretely:
- **B1**: the `download_edgemlsdk_release_artifacts` list contains an
  `aws s3api head-bucket --bucket panorama-sdk-v2-artifacts --expected-bucket-owner …`
  entry before the `panorama-sdk-v2-artifacts` sync and an
  `aws s3api head-bucket --bucket edgeml-sdk-longevity-tests --expected-bucket-owner …`
  entry before the three longevity accesses; the owner values are `shlex.quote`'d;
  the existing `aws s3 cp`/`aws s3 sync` strings are unchanged (Req 2.1).
- **B2**: `publish.sh` resolves `ARTIFACT_BUCKET`, runs a `head-bucket` preflight
  with `|| exit 1`, and uploads to `s3://${ARTIFACT_BUCKET}/…` (Req 2.2).
- **B3**: `publish.sh` resolves `DOCS_BUCKET`, runs a `head-bucket` preflight
  inside the `if [ -d "./sphinx" ]` guard, and syncs to `s3://${DOCS_BUCKET}/…`
  (Req 2.3).
- **B4**: `index.rst` has an ownership `.. note::` and a documented `head-bucket`
  preflight in the dependency and release `code-block`s (Req 2.4).
- **B5**: `s3.rst`'s config sample `bucket` value is `<your-bucket-name>` with a
  prerequisite `.. note::` (Req 2.5).
- **B6**: the notebook's `old_prefix` derives from `sample_data_bucket` and a
  prerequisite markdown cell precedes the `seg_manifest` cell (Req 2.6).
- **B7**: `s3_squat_audit.disallowed_hits() == []` (Req 2.7).

### Preservation Checking

**Goal**: For all inputs where the bug condition does NOT hold, the fixed code
produces the same result as the original — `F(X) = F'(X)`.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT originalFunction(input) = fixedFunction(input)
END FOR
```

**Testing Approach**: Property-based testing where the domain is generatable
(owner account matches / mismatches the expected owner; env vars set / unset);
capture baseline behavior on the **unfixed** code first, then assert the fixed
code matches. The core preservation argument is that **`aws s3api head-bucket
--expected-bucket-owner <acct>` is a no-op when the bucket IS owned by `<acct>`**
(returns `200`, exit 0), so every legitimate download / upload proceeds exactly
as before; the preflight only changes behavior for the buggy (squatted / wrong-
owner) inputs.

**Property-based test plans:**
- **PBT 1 — owner matches vs owner mismatch (B1, B2, B3)**: model the preflight
  as `preflight(bucket_owner, expected_owner) -> pass|fail_closed`. Generate
  `(bucket_owner, expected_owner)` account-ID pairs. Invariants: when
  `bucket_owner == expected_owner`, the preflight passes and the subsequent
  download/upload runs identically to `F` (no-op preservation); when
  `bucket_owner != expected_owner`, the fixed code fails closed (aborts before
  `dpkg -i`/`pip install`/upload) whereas `F` would have proceeded — the
  DIFFERENCE is exactly the mismatched-owner (squatting) inputs.
- **PBT 2 — env var set vs unset (B2, B3)**: model bucket resolution as
  `resolve(env_value) -> env_value or default_literal`. Generate `env_value ∈
  {unset, "panorama-sdk-v2-artifacts", "edgeml-sdk-docs", "my-account-bucket",
  random}`. Invariants: when unset, `ARTIFACT_BUCKET`/`DOCS_BUCKET` resolve to the
  current literals and the emitted `s3://…` targets are byte-for-byte identical to
  `F`; when set, they resolve to the provided value (intended new behavior). The
  DIFFERENCE for the unset case is empty (perfect preservation).
- **PBT 3 — deploy.py SSM command-string equality (B1)**: model the SSM list
  builder as a pure function of `(args)`. Generate legitimate `args`
  (allowlisted `region`/`platform`/`ubuntu_version`/…). Invariant: the set of
  emitted entries equals the sibling **secrets-baseline** list PLUS exactly the
  two `aws s3api head-bucket …` entries — every pre-existing entry (the four
  `aws s3 cp`/`aws s3 sync`, the `export`, `mkdir`s, `ecr`/`docker`) is
  byte-for-byte identical, and the `# nosec B105` line and `shlex.quote`'d args
  are preserved.
- **PBT 4 — notebook manifest rewrite unchanged (B6)**: model
  `update_manifest_paths(entries, old_prefix, new_prefix)`. Generate manifest
  entries with `source-ref`/`anomaly-mask-ref` values that do / don't start with
  the prefix. Invariant: the fixed cell (with `old_prefix` derived from
  `sample_data_bucket = "lookoutvision-us-east-1-0e205be246"`) computes the exact
  same `old_prefix` string as `F`, so `update_manifest_paths` produces identical
  output for every input (perfect preservation of the rewrite logic).

**Example-based preservation cases:**
1. **B1 legitimate deploy**: with `artifacts_bucket_owner` / `longevity_bucket_owner`
   set to the correct accounts, the two `head-bucket` preflights return `200`, and
   the SDK artifacts + configs download and `dpkg -i`/`pip install` identically to
   the pre-fix deploy.
2. **B2/B3 legitimate publish**: `publish.sh` with env vars unset (or set to the
   current literals) and `EXPECTED_BUCKET_OWNER` = the publishing account uploads
   the same versioned+`latest` `.deb`/`.whl` and syncs docs to the same path.
3. **B4/B5 doc rendering**: `index.rst`/`s3.rst` render with all original prose /
   config structure intact; only the note / placeholder / preflight line differ
   (diff-verified).
4. **B6 notebook run**: `update_manifest_paths` rewrites the same manifest entries
   (those under the L4V sample prefix) to `s3_uri`; entries not under the prefix
   are left unchanged, exactly as before.
5. **Out-of-scope untouched (Req 3.8)**: the vendored
   `src/backend/edgemlsdk/edgemlsdk/…` duplicate, `cdk.out/**`, and the sibling
   spec files are byte-for-byte unchanged.

### Unit Tests

- **B1**: parse the `download_edgemlsdk_release_artifacts` list (import `main`'s
  builder or `ast`-inspect the module); assert the two `head-bucket` preflight
  entries exist, precede their respective `aws s3` entries, carry
  `--expected-bucket-owner`, and are `shlex.quote`'d; assert the four existing
  `aws s3 cp`/`aws s3 sync` strings and the `# nosec B105` line are byte-for-byte
  equal to the pre-fix golden.
- **B2/B3**: `bash -n publish.sh` (syntax); assert `ARTIFACT_BUCKET`/`DOCS_BUCKET`
  default to the current literals when unset; assert a `head-bucket
  --expected-bucket-owner` preflight with `|| exit 1` precedes each upload group;
  assert the versioned+`latest` uploads and the `if [ -d "./sphinx" ]` guard are
  intact.
- **B4**: parse `index.rst`; assert a `.. note::` precedes the dependency
  `code-block` and an `aws s3api head-bucket --expected-bucket-owner` line is in
  the dependency + release blocks; assert the four `dpkg -i` and the `pip install`
  lines are unchanged.
- **B5**: parse `s3.rst`; assert the config `bucket` value is `<your-bucket-name>`
  and a `.. note::` is present; assert every other config key unchanged.
- **B6**: `nbformat.read` / `json.load` the notebook; assert it is valid, a
  prerequisite markdown cell precedes the `seg_manifest` cell, `sample_data_bucket`
  is defined once, `old_prefix` is an f-string over it, and no bare
  `lookoutvision-` literal remains on the download path outside the documented
  variable.
- **B7**: `s3_squat_audit.disallowed_hits() == []`; `run_audit()` excludes the
  vendored duplicate and `cdk.out`.

### Property-Based Tests

- PBT 1 (owner matches vs mismatch) — invariant: preflight no-op on match,
  fail-closed on mismatch; legitimate flows preserved.
- PBT 2 (env var set vs unset) — invariant: unset resolves to current literal
  (byte-identical target); set resolves to provided value.
- PBT 3 (deploy.py SSM list equality) — invariant: emitted list == secrets
  baseline + exactly the two `head-bucket` entries.
- PBT 4 (notebook rewrite unchanged) — invariant: `update_manifest_paths` output
  identical for all inputs; `old_prefix` string unchanged.

### Integration Tests

- **B1 deploy dry-run (gated)**: in a staging account, run the longevity deploy
  against correctly-owned buckets and assert the SSM batch succeeds (preflight
  `200`); then point `ARTIFACTS_BUCKET_OWNER` at a wrong account and assert the
  batch aborts at the preflight before any `dpkg -i` (the intended DIFFERENCE).
- **B2/B3 publish replay (gated)**: run `publish.sh` in a staging account with
  `EXPECTED_BUCKET_OWNER` correct (uploads succeed) and incorrect (aborts at
  preflight); verify the uploaded objects land at the same versioned+`latest`
  keys and docs path as the pre-fix baseline.
- **B4/B5 docs build**: build the Sphinx docs and assert the note / placeholder
  render; grep the built HTML for the absence of a bare
  `"bucket": "panorama-sdk-v2-artifacts"` config value.
- **B6 notebook execution**: run the `seg_manifest` cell against a valid
  `sample_data_bucket` and assert the manifest is rewritten and uploaded exactly
  as before.
- **B7 audit gate in CI**: run `s3_squat_audit.py` in `build-custom.sh` — it fails
  if any predictable-bucket access without an owner assertion / placeholder / note
  reappears in in-scope source.

**Rollback plan** (per the bugfix.md commitments): each of B1–B6 is a separate
commit / task. If a legitimate flow breaks after a fix (e.g. a wrong
`--expected-bucket-owner` account for a given deployment), the specific change can
be reverted independently — isolated to a single file's diff — without touching
the other five fixes or the sibling remediation branches
(`security-injection-deserialization-fixes`,
`security-secrets-credentials-jwt-fixes`, `security-iam-authorization-fixes`).
