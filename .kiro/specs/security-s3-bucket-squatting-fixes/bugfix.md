# Bugfix Requirements Document

## Introduction

A code security review of the DefectDetectionApplication (DDA) — the AWS "DDA
Code Review" scan captured in `security-findings-report.json` at the repo root
(63 findings total) — surfaced 6 HIGH-severity **S3 Bucket Squatting** findings
that are the subject of this spec. Every finding is a hardcoded, predictable S3
bucket name that is read-from, written-to, or shown in copy-pasteable
documentation/notebook code, **without** any bucket-owner assertion, integrity
verification, or placeholder. Because the bucket names are guessable and the
accesses are unverified, an attacker who creates ("squats") a same-named bucket
in a region/account where the legitimate one does not exist — or who otherwise
gets a request routed to a bucket they control — could:

1. **Serve malicious artifacts on the read path** — the longevity deploy and
   the SDK install documentation download SDK release artifacts, C++ SDK
   dependencies, and test configs from predictable buckets and then
   `dpkg -i` / `pip install` / execute them. A squatted bucket could serve a
   trojaned `.deb` / `.whl` that is installed with elevated privileges.
2. **Receive exfiltrated data / silently fail on the write path** — the
   `publish.sh` distribution script uploads freshly built `.deb` / `.whl`
   packages and Sphinx docs to predictable buckets; if the real bucket is
   deleted and re-squatted, uploads could land in an attacker-owned bucket.
3. **Mislead operators via copy-pasteable examples** — the S3 Message Broker
   docs and the SageMaker training notebook embed real, guessable bucket names
   that users copy verbatim, propagating the squatting exposure into customer
   deployments.

This spec ("S3 Bucket Squatting Prevention" — the fifth remediation group in
the AWS code-review sequence, stacked after `security-iam-authorization-fixes`)
is scoped **strictly** to the 6 findings enumerated below (labelled B1–B6)
plus a repository audit gate (B7). It uses the SAME bug-condition methodology,
EARS format, and Property 1 (Fix Checking) / Property 2 (Preservation) framing
as the sibling specs `security-injection-deserialization-fixes` (findings
#1–#8), `security-secrets-credentials-jwt-fixes` (findings S1–S9), and
`security-iam-authorization-fixes` (findings I1–I17).

### The user's remediation choice: Option A (integrity-verification + parameterization)

The buckets in scope fall into two ownership classes, which drive two different
mitigations. This spec adopts the user's **Option A** decision — owner
assertion / integrity verification and parameterization, **NOT** a naive rename
of the AWS-managed buckets:

- **AWS-managed buckets that DDA does not own** — `panorama-sdk-v2-artifacts`
  (the AWS Panorama SDK distribution bucket) and `lookoutvision-us-east-1-0e205be246`
  (an AWS public sample dataset bucket). Renaming these is deliberately **NOT**
  done because DDA cannot rename a bucket it does not control. The mitigation on
  the **read path** is an `--expected-bucket-owner` assertion (or an equivalent
  integrity check) so that a squatted bucket in an account other than the
  expected AWS account fails closed.
- **Team-owned distribution buckets** — `panorama-sdk-v2-artifacts` (also
  written-to by `publish.sh`), `edgeml-sdk-docs`, and `edgeml-sdk-longevity-tests`.
  On the **write path** these are parameterized via an env var (defaulting to
  the current value, with guidance to set an account-scoped name) **and** get an
  `--expected-bucket-owner` assertion so a squatted target fails closed.

### Glossary

- **Squatting / bucket sniping** — an attacker pre-creates or re-creates a
  bucket with a predictable name (typically after the legitimate one is deleted,
  or in a region/partition where it never existed) so that unverified accesses
  to that name are served by the attacker's bucket.
- **`--expected-bucket-owner <ACCOUNT>`** — an AWS CLI / API argument that makes
  the request fail with `403 Access Denied` unless the target bucket is owned by
  the specified account. It is a **no-op** when the bucket *is* owned by the
  expected account, so it does not change behavior for legitimate flows.
- **AWS-managed bucket** — a bucket owned by an AWS service team (Panorama SDK
  distribution, Lookout for Vision samples), not by DDA. DDA cannot rename it.
- **Team-owned bucket** — a bucket owned by the DDA / edgemlsdk team's AWS
  account (release artifacts, docs, longevity test configs).
- **Read path** — code/docs that *download* from a bucket and then install or
  execute the downloaded artifact.
- **Write path** — code that *uploads* build outputs / docs to a bucket.
- **F** — the original (unfixed) code/doc/notebook, where the S3 access uses a
  hardcoded predictable bucket with no owner assertion / integrity check /
  placeholder.
- **F'** — the fixed code/doc/notebook, where every in-scope S3 access asserts
  the expected bucket owner (or verifies integrity), team-owned write buckets
  are parameterized, and doc/notebook references are placeholders or owner-noted.

### Explicitly out of scope (handled elsewhere, or fundamentally out of scope)

- **The vendored / generated duplicate SDK subtree** —
  `src/backend/edgemlsdk/edgemlsdk/...` is a vendored/generated copy of the
  edgemlsdk SDK. It contains its own `deploy.py`, its own message-broker tests,
  and other copies that reference the same predictable bucket names, but it
  regenerates from the maintained source at `src/edgemlsdk/src/...`. Only the
  maintained source paths listed as B1–B6 are to be fixed; the vendored copy is
  NOT touched. (Note: the vendored `deploy.py` still interpolates raw argparse
  args and lacks the sibling secrets spec's `shlex.quote` hardening — further
  confirming it is an unmaintained generated copy.)
- **CDK synth output** — `edge-cv-portal/infrastructure/cdk.out/**` and any
  other build artifact directory.
- **Renaming the AWS-managed buckets** (`panorama-sdk-v2-artifacts`,
  `lookoutvision-*`) — deliberately NOT done, because DDA does not own them; per
  the user's Option A choice the mitigation is owner-assertion / integrity, not
  rename.
- **The already-remediated batches** — injection / unsafe-deserialization
  (findings #1–#8, `security-injection-deserialization-fixes`); secrets,
  credentials & JWT/token handling (findings S1–S9,
  `security-secrets-credentials-jwt-fixes`); and IAM & Authorization (findings
  I1–I17, `security-iam-authorization-fixes`). In particular, the
  `secret_name = "edgeml-sdk-longevity-tests"  # nosec B105` line and the
  `shlex.quote`'d SSM command construction in `deploy.py` were introduced by the
  secrets spec (S2 / S8); this spec's B1 fix is the *squatting / integrity*
  concern, distinct from the B105 secret-name concern, and MUST preserve those
  existing lines byte-for-byte.
- **Any other finding class from the 63-finding report** not listed as B1–B6
  below.

### The findings and their real source locations

All 6 findings are in the maintained edgemlsdk SDK subtree
(`src/edgemlsdk/src/...`), except B6 which is the repo-root SageMaker notebook.
Scanner line numbers are given with `~` because they drift.

**Read-path integrity fixes (AWS-managed buckets — add owner assertion / integrity, do NOT rename)**

| # | File | Loc | Finding | Fix |
|---|------|-----|---------|-----|
| B1 | `src/edgemlsdk/src/test/longevity/deploy.py` | ~157 / ~175 / ~176–178 | The longevity deploy builds `AWS-RunShellScript` SSM commands that `aws s3 sync s3://panorama-sdk-v2-artifacts/release/...` (SDK release artifacts) and `aws s3 cp s3://edgeml-sdk-longevity-tests/...` (`longevity.json`, `delegates.json`, mqtt configs), then `dpkg -i` / `pip install` them on the deployed EC2 instance. Bucket names are hardcoded and predictable; downloads are unverified. | Add an `--expected-bucket-owner <ACCOUNT>` assertion to the `aws s3 cp` / `aws s3 sync` calls in the SSM command list. Introduce the expected owner account as a parameter / env var (e.g. `ARTIFACTS_BUCKET_OWNER` for `panorama-sdk-v2-artifacts` and the longevity account for `edgeml-sdk-longevity-tests`) rather than renaming the AWS-managed bucket. **Implementation-time check:** verify the AWS CLI supports `--expected-bucket-owner` on the high-level `aws s3 cp` / `aws s3 sync` commands; if not, fall back to an `aws s3api head-bucket --expected-bucket-owner` preflight or a post-download checksum step. Preserve the exact download / command semantics — the sibling secrets spec's `shlex.quote`'d args and the `# nosec B105` lines must remain byte-for-byte. |

**Write-path parameterization (team-owned distribution buckets — env var + owner assertion)**

| # | File | Loc | Finding | Fix |
|---|------|-----|---------|-----|
| B2 | `src/edgemlsdk/src/utilities/publish.sh` | ~23 | `aws s3 cp *.deb s3://panorama-sdk-v2-artifacts/release/...` and the matching `.whl` uploads publish freshly built packages to a predictable bucket with no owner assertion. | Parameterize the bucket via an env var (e.g. `ARTIFACT_BUCKET`, defaulting to `panorama-sdk-v2-artifacts` with guidance to set an account-scoped name) AND add `--expected-bucket-owner` on each upload so a squatted bucket fails closed. Preserve the release-path layout and the `latest`/versioned dual-upload semantics. |
| B3 | `src/edgemlsdk/src/utilities/publish.sh` | ~31 | `aws s3 sync ./sphinx s3://edgeml-sdk-docs/edgeml-sdk/v1/...` uploads Sphinx docs to a predictable bucket with no owner assertion. | Same pattern — parameterize via an env var (e.g. `DOCS_BUCKET`, defaulting to `edgeml-sdk-docs`) AND add `--expected-bucket-owner`. Preserve the `edgeml-sdk/v1/$major_minor/` path layout and the `if [ -d "./sphinx" ]` guard. |

**Documentation / notebook placeholder + verification-note fixes**

| # | File | Loc | Finding | Fix |
|---|------|-----|---------|-----|
| B4 | `src/edgemlsdk/src/docs/source/index.rst` | ~42 | Bash `code-block` examples that `aws s3 cp s3://panorama-sdk-v2-artifacts/dependencies/.../*.deb ./` then `dpkg -i` them (aws-c-iot, aws-crt-cpp, aws-iot-device-sdk-cpp-v2, aws-sdk-cpp), plus the `PanoramaSDK.deb` / `panorama-1.0-py3-none-any.whl` release downloads. Copy-pasteable, unverified. | Add a preceding note that these commands pull from the AWS-managed Panorama SDK distribution bucket and that users should verify bucket ownership / artifact integrity; add `--expected-bucket-owner` to the documented `aws s3 cp` commands as the recommended pattern. Doc-only (no executable code changes). |
| B5 | `src/edgemlsdk/src/docs/source/components/message_broker/s3.rst` | ~87 | The Message Broker Config JSON sample contains `"bucket": "panorama-sdk-v2-artifacts"`, a real guessable name users copy verbatim. | Replace the example bucket value with an obvious placeholder such as `"<your-bucket-name>"` and add a prerequisite note that users must create / own the bucket they publish to. Doc-only. |
| B6 | `DDA_SageMaker_Model_Training_and_Compilation.ipynb` (repo root) | ~141 | The segmentation-manifest cell hardcodes `old_prefix = 's3://lookoutvision-us-east-1-0e205be246/getting-started/'` — an AWS public Lookout-for-Vision sample bucket. `update_manifest_paths` rewrites manifest entries that start with this prefix to the user's own `s3://{bucket}/...`; entries that do not match are left pointing at the AWS-owned sample bucket, and the value is copy-pasteable. | Parameterize the prefix to a `user_provided_bucket` (or `sample_data_bucket`) variable and add a preceding prerequisite markdown cell instructing users to create their own bucket / verify the AWS-owned sample bucket before use. Notebook-only. |

**Repository audit gate**

| # | File | Loc | Finding | Fix |
|---|------|-----|---------|-----|
| B7 | (in-scope files only) | — | No runnable check exists that asserts the fixes above are in place; regressions could re-introduce a hardcoded predictable bucket read/write/example without an owner assertion / placeholder / documented exception. | Add a runnable audit that greps the in-scope files (B1–B6 files only) for the predictable bucket names (`panorama-sdk-v2-artifacts`, `edgeml-sdk-docs`, `edgeml-sdk-longevity-tests`, `lookoutvision-*`) used in an `aws s3 cp` / `aws s3 sync` / message-broker config / notebook download **without** an adjacent `--expected-bucket-owner` / documented placeholder / `# nosec`, and asserts zero disallowed hits minus documented exceptions. Excludes the vendored `src/backend/edgemlsdk/edgemlsdk/...` duplicate and `cdk.out`. |

### Testability + Rollback commitments

The 6 fixes span three surface types with three verification approaches:

- **Shell / Python command construction (B1, B2, B3)** are verifiable via
  grep / parse assertions against the emitted command strings: assert that every
  in-scope `aws s3 cp` / `aws s3 sync` line carries an `--expected-bucket-owner`
  argument (or is preceded by an `aws s3api head-bucket --expected-bucket-owner`
  preflight / a checksum step), and that the write-path bucket names are read
  from an env var with the documented default. For **B1**, the `deploy.py` SSM
  command list MUST be verified to be **byte-for-byte identical to the sibling
  secrets baseline** except for the added `--expected-bucket-owner` tokens — the
  `shlex.quote`'d argument interpolation and the `# nosec B105` lines are
  preserved unchanged.
- **`--expected-bucket-owner` CLI-support caveat (implementation-time check).**
  Whether `--expected-bucket-owner` is accepted on the *high-level* `aws s3 cp`
  / `aws s3 sync` commands (versus only on the low-level `aws s3api` commands)
  is a version-dependent detail that MUST be verified at implementation time. If
  the high-level commands do not accept it, the fix MUST fall back to an
  `aws s3api head-bucket --expected-bucket-owner <ACCOUNT>` preflight before the
  download, or a post-download checksum verification. The design phase records
  which mechanism was chosen per call site.
- **Documentation (B4, B5)** are verifiable by extracting the affected
  `code-block` fences and asserting the presence of the ownership/verification
  note and, for B4, the `--expected-bucket-owner` pattern in the documented
  commands; for B5, asserting the JSON sample's `bucket` value is a placeholder
  (`<...>`) and no longer the literal `panorama-sdk-v2-artifacts`.
- **Notebook (B6)** is verifiable by parsing the `.ipynb` JSON and asserting the
  `old_prefix` (or its replacement) is derived from a `user_provided_bucket` /
  `sample_data_bucket` variable rather than a hardcoded `lookoutvision-*`
  literal, and that a prerequisite markdown cell precedes the download.
- **Rollback plan:** each of B1–B6 is a **separate commit / task** in this
  spec's task breakdown. If a legitimate flow breaks after a fix (e.g. the
  `--expected-bucket-owner` account value is wrong for a given deployment), the
  specific change can be reverted independently — isolated to a single file's
  diff — without touching the other 5 fixes or the sibling remediation branches.

### Bug Condition and Properties

The bug-condition methodology frames this fix as follows.

**Bug Condition `C(X)`** — identifies the inputs/code paths that trigger the
defect. Here the "input" is any S3 access (read, write, or copy-pasteable
example) in an in-scope file that targets a hardcoded, predictable bucket name
without an owner assertion / integrity check / placeholder:

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type S3Access   // an aws s3 cp/sync call, a message-broker
                              // config entry, or a notebook download prefix
                              // in an in-scope file (B1-B6)
  OUTPUT: boolean

  // True when a hardcoded, predictable bucket name is read-from / written-to /
  // shown in copy-pasteable docs WITHOUT a bucket-owner assertion, integrity
  // verification, or placeholder:
  //   - the bucket name is a known predictable literal
  //     (panorama-sdk-v2-artifacts, edgeml-sdk-docs,
  //      edgeml-sdk-longevity-tests, lookoutvision-*); AND
  //   - the access is a read that then installs/executes the artifact, OR a
  //     write of build outputs, OR a copy-pasteable doc/notebook reference; AND
  //   - there is NO adjacent --expected-bucket-owner assertion / head-bucket
  //     preflight / checksum step (for read/write), NO env-var
  //     parameterization (for team-owned writes), and NO placeholder /
  //     ownership note (for docs/notebook).
  RETURN predictableBucketLiteral(X)
      AND (readThenInstall(X) OR buildOutputWrite(X) OR copyPasteableExample(X))
      AND NOT (ownerAsserted(X) OR integrityVerified(X)
               OR parameterizedWithOwnerAssertion(X)
               OR placeholderOrOwnershipNoted(X))
END FUNCTION
```

**Fix Property `P` (Property 1 — Fix Checking)** — desired behavior for all
buggy inputs after the fix `F'`:

```pascal
// Property 1: Fix Checking - every in-scope S3 access is squatting-resistant
FOR ALL X WHERE isBugCondition(X) DO
  result <- F'(X)
  ASSERT ownerAssertedOrIntegrityVerified(result)
     // Read/write call sites (B1, B2, B3) carry --expected-bucket-owner
     // on the aws s3 cp/sync command, OR an aws s3api head-bucket
     // --expected-bucket-owner preflight, OR a post-download checksum step,
     // so a squatted bucket owned by another account fails closed.
  ASSERT parameterizedWhereTeamOwned(result)
     // Team-owned write buckets (B2 panorama-sdk-v2-artifacts, B3
     // edgeml-sdk-docs) are read from an env var (ARTIFACT_BUCKET /
     // DOCS_BUCKET) defaulting to the current value, with guidance to set an
     // account-scoped name.
  ASSERT placeholderOrOwnerNoted(result) WHEN X is a doc/notebook reference
     // B4: an ownership/verification note precedes the documented commands and
     //     --expected-bucket-owner is shown as the recommended pattern.
     // B5: the JSON sample bucket value is a placeholder (<your-bucket-name>).
     // B6: the notebook prefix is a user_provided_bucket variable preceded by
     //     a prerequisite markdown cell.
END FOR
```

**Preservation Property (Property 2 — Preservation Checking)** — for every
input that does NOT trigger the bug condition (i.e. every legitimate flow
against the correctly-owned buckets), the fixed code behaves identically to the
original code `F`:

```pascal
// Property 2: Preservation Checking - no behavior change for legitimate flows
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
     // --expected-bucket-owner is a NO-OP when the bucket IS owned by the
     // expected account, so legitimate downloads/uploads against the correctly
     // owned buckets still succeed byte-for-byte.
     // Env-var parameterization DEFAULTS to the current bucket value, so a
     // deployment that does not set the env var behaves identically.
     // The deploy.py SSM command strings remain byte-for-byte identical to the
     // sibling secrets baseline except for the added --expected-bucket-owner
     // tokens; the shlex.quote'd arg interpolation and the # nosec B105 lines
     // are preserved.
     // Doc prose and code-block layout, the message-broker config structure,
     // and the notebook's manifest-rewrite logic are otherwise unchanged.
     // Every file / line in the in-scope files that is NOT one of B1-B6, and
     // the entire vendored src/backend/edgemlsdk/edgemlsdk/... duplicate,
     // remain byte-for-byte identical.
END FOR
```

- **F**: the original (unfixed) code/doc/notebook, where the S3 access uses a
  hardcoded predictable bucket with no owner assertion / integrity check /
  parameterization / placeholder.
- **F'**: the fixed code/doc/notebook, where every in-scope S3 access asserts
  the expected bucket owner (or verifies integrity), team-owned write buckets
  are parameterized with an owner assertion, and doc/notebook references are
  placeholders or owner-noted.

Where the input domain is generatable (e.g. bucket-owner account IDs that match
vs. do not match the expected owner; env-var values set vs. unset;
manifest-entry prefixes that match vs. do not match the sample prefix),
**property-based testing** is emphasized in the design phase: generate accesses
whose target is owned by the expected account (assert they succeed unchanged)
vs. owned by a different account (assert they fail closed); generate
`publish.sh` invocations with `ARTIFACT_BUCKET` / `DOCS_BUCKET` set vs. unset
and assert the resolved bucket + owner-assertion shape; parse the notebook and
assert no hardcoded `lookoutvision-*` literal remains on the download path.

## Bug Analysis

### Current Behavior (Defect)

The application reads from, writes to, and documents predictable, hardcoded S3
bucket names without asserting the bucket owner, verifying artifact integrity,
parameterizing team-owned targets, or using placeholders — so a squatted
same-named bucket could serve malicious artifacts (read path), receive
build outputs / exfiltrated data (write path), or propagate the exposure into
customer deployments via copy-pasteable examples.

1.1 WHEN `main(args)` in `src/edgemlsdk/src/test/longevity/deploy.py` (~line
157; scanner reported ~175 / ~176–178 with drift) builds the
`download_edgemlsdk_release_artifacts` list of `AWS-RunShellScript` SSM commands
THEN the system emits `aws s3 sync s3://panorama-sdk-v2-artifacts/release/...`
and `aws s3 cp s3://edgeml-sdk-longevity-tests/longevity.json` /
`.../delegates.json` / `aws s3 sync s3://edgeml-sdk-longevity-tests/<source>`
commands that download SDK release artifacts and test configs from hardcoded,
predictable buckets, and subsequently `dpkg -i` / `pip install` them, with **no**
`--expected-bucket-owner` assertion, `head-bucket` preflight, or checksum step —
a squatted bucket could serve a trojaned `.deb` / `.whl`.

1.2 WHEN `src/edgemlsdk/src/utilities/publish.sh` (~line 23) runs THEN the
system executes `aws s3 cp *.deb s3://panorama-sdk-v2-artifacts/release/...`
(versioned and `latest`) and the corresponding `.whl` uploads against the
hardcoded, predictable `panorama-sdk-v2-artifacts` bucket, with **no** env-var
parameterization and **no** `--expected-bucket-owner` assertion — a squatted
bucket could receive the freshly built packages.

1.3 WHEN `src/edgemlsdk/src/utilities/publish.sh` (~line 31) runs and `./sphinx`
exists THEN the system executes `aws s3 sync ./sphinx
s3://edgeml-sdk-docs/edgeml-sdk/v1/$major_minor/` against the hardcoded,
predictable `edgeml-sdk-docs` bucket, with **no** env-var parameterization and
**no** `--expected-bucket-owner` assertion.

1.4 WHEN a reader follows the `src/edgemlsdk/src/docs/source/index.rst` (~line
42) installation instructions THEN the documentation instructs them to run
`aws s3 cp s3://panorama-sdk-v2-artifacts/dependencies/.../*.deb ./` (four C++
SDK dependency packages) followed by `dpkg -i`, and the `PanoramaSDK.deb` /
`panorama-1.0-py3-none-any.whl` release downloads, from the hardcoded,
predictable `panorama-sdk-v2-artifacts` bucket, with **no** note about verifying
bucket ownership / artifact integrity and **no** `--expected-bucket-owner`
pattern shown.

1.5 WHEN a reader copies the Message Broker Config Sample in
`src/edgemlsdk/src/docs/source/components/message_broker/s3.rst` (~line 87) THEN
the documentation presents a JSON `s3_message_options` block whose `"bucket"`
value is the real, guessable literal `"panorama-sdk-v2-artifacts"`, with **no**
placeholder and **no** prerequisite note that the user must create / own the
bucket they publish to.

1.6 WHEN the `DDA_SageMaker_Model_Training_and_Compilation.ipynb` notebook
(repo root, ~line 141) runs the segmentation-manifest cell THEN it sets
`old_prefix = 's3://lookoutvision-us-east-1-0e205be246/getting-started/'` — a
hardcoded AWS public sample bucket — and `update_manifest_paths` only rewrites
manifest entries that start with that prefix; entries that do not match remain
pointed at the AWS-owned sample bucket, and the guessable literal is
copy-pasteable, with **no** parameterization and **no** prerequisite note to
create one's own bucket / verify the AWS-owned sample bucket.

1.7 WHEN the repository is audited for the bug-condition patterns in the
in-scope files (B1–B6) — a predictable bucket literal
(`panorama-sdk-v2-artifacts`, `edgeml-sdk-docs`, `edgeml-sdk-longevity-tests`,
`lookoutvision-*`) used in an `aws s3 cp` / `aws s3 sync` / message-broker
config / notebook download without an adjacent `--expected-bucket-owner` /
documented placeholder / `# nosec` — THEN the unfixed tree contains the
disallowed occurrences above with no documented, justified exception.

### Expected Behavior (Correct)

After the fix, every in-scope S3 access is squatting-resistant: read and write
call sites assert the expected bucket owner (or verify integrity), team-owned
write buckets are parameterized via env vars defaulting to the current value,
and documentation / notebook references are placeholders or carry an ownership /
verification note.

2.1 WHEN `main(args)` in `deploy.py` (~line 157) builds the
`download_edgemlsdk_release_artifacts` SSM command list THEN the system SHALL
add an `--expected-bucket-owner <ACCOUNT>` assertion to each `aws s3 cp` /
`aws s3 sync` command (sourcing the expected owner from a parameter / env var
such as `ARTIFACTS_BUCKET_OWNER` for `panorama-sdk-v2-artifacts` and the
longevity account for `edgeml-sdk-longevity-tests`), rather than renaming the
AWS-managed bucket; if the high-level `aws s3` commands do not accept
`--expected-bucket-owner`, the system SHALL instead emit an
`aws s3api head-bucket --expected-bucket-owner <ACCOUNT>` preflight or a
post-download checksum step. The `shlex.quote`'d argument interpolation and the
`# nosec B105` lines introduced by the sibling secrets spec SHALL be preserved
byte-for-byte.

2.2 WHEN `publish.sh` (~line 23) runs THEN the system SHALL read the target
bucket from an env var (e.g. `ARTIFACT_BUCKET`, defaulting to
`panorama-sdk-v2-artifacts` with guidance to set an account-scoped name) AND
SHALL add `--expected-bucket-owner` to each `.deb` / `.whl` upload so a squatted
bucket fails closed, while preserving the versioned + `latest` release-path
layout.

2.3 WHEN `publish.sh` (~line 31) runs and `./sphinx` exists THEN the system
SHALL read the docs bucket from an env var (e.g. `DOCS_BUCKET`, defaulting to
`edgeml-sdk-docs`) AND SHALL add `--expected-bucket-owner` to the
`aws s3 sync ./sphinx ...` upload, while preserving the
`edgeml-sdk/v1/$major_minor/` path layout and the `if [ -d "./sphinx" ]` guard.

2.4 WHEN a reader follows the `index.rst` (~line 42) installation instructions
THEN the documentation SHALL present a preceding note that the commands pull
from the AWS-managed Panorama SDK distribution bucket and that users should
verify bucket ownership / artifact integrity, AND SHALL show
`--expected-bucket-owner` on the documented `aws s3 cp` commands as the
recommended pattern, while preserving the surrounding prose and the `dpkg -i` /
`pip install` steps.

2.5 WHEN a reader copies the Message Broker Config Sample in `s3.rst` (~line 87)
THEN the documentation SHALL present the JSON `s3_message_options` `"bucket"`
value as an obvious placeholder (e.g. `"<your-bucket-name>"`) AND SHALL add a
prerequisite note that users must create / own the bucket they publish to, while
preserving the rest of the sample structure.

2.6 WHEN the `DDA_SageMaker_Model_Training_and_Compilation.ipynb` notebook (~line
141) runs the segmentation-manifest cell THEN the system SHALL derive the
manifest prefix from a `user_provided_bucket` (or `sample_data_bucket`) variable
rather than the hardcoded `lookoutvision-us-east-1-0e205be246` literal, AND SHALL
be preceded by a prerequisite markdown cell instructing users to create their own
bucket / verify the AWS-owned sample bucket before use, while preserving the
`update_manifest_paths` rewrite logic.

2.7 WHEN the repository is audited for the bug-condition patterns in the
in-scope files (B1–B6) THEN the system SHALL contain no remaining disallowed
occurrence — no predictable bucket literal (`panorama-sdk-v2-artifacts`,
`edgeml-sdk-docs`, `edgeml-sdk-longevity-tests`, `lookoutvision-*`) used in an
`aws s3 cp` / `aws s3 sync` / message-broker config / notebook download without
an adjacent `--expected-bucket-owner` / `head-bucket` preflight / documented
placeholder / ownership note / `# nosec` — other than occurrences carrying a
documented, justified exception. The audit SHALL be runnable, SHALL assert zero
disallowed hits minus documented exceptions, and SHALL exclude the vendored
`src/backend/edgemlsdk/edgemlsdk/...` duplicate and `cdk.out`.

### Unchanged Behavior (Regression Prevention)

All legitimate flows against the correctly-owned buckets must continue to work
exactly as before. For every input that does NOT trigger the bug condition —
i.e. every download / upload / example that targets a bucket owned by the
expected account — the fixed system must behave identically to the original.
Every file / line in the in-scope files that is NOT one of B1–B6, and the entire
vendored `src/backend/edgemlsdk/edgemlsdk/...` duplicate, must remain
byte-for-byte identical.

3.1 WHEN the longevity deploy downloads SDK release artifacts and test configs
from `panorama-sdk-v2-artifacts` and `edgeml-sdk-longevity-tests` that ARE owned
by the expected accounts THEN the system SHALL CONTINUE TO download and
`dpkg -i` / `pip install` them successfully — `--expected-bucket-owner` is a
no-op when the bucket is owned by the expected account — and the SSM command
strings SHALL remain byte-for-byte identical to the sibling secrets baseline
except for the added owner-assertion tokens, with the `shlex.quote`'d args and
`# nosec B105` lines preserved.

3.2 WHEN `publish.sh` runs without `ARTIFACT_BUCKET` set (or with it set to
`panorama-sdk-v2-artifacts`) and the target bucket IS owned by the expected
account THEN the system SHALL CONTINUE TO upload the `.deb` / `.whl` packages to
the same versioned and `latest` release paths successfully and identically.

3.3 WHEN `publish.sh` runs without `DOCS_BUCKET` set (or with it set to
`edgeml-sdk-docs`), `./sphinx` exists, and the target bucket IS owned by the
expected account THEN the system SHALL CONTINUE TO sync the docs to the same
`edgeml-sdk/v1/$major_minor/` path successfully and identically.

3.4 WHEN a reader follows the updated `index.rst` instructions against the
correctly-owned `panorama-sdk-v2-artifacts` distribution bucket THEN the system
SHALL CONTINUE TO download and install the C++ SDK dependencies, the
`PanoramaSDK.deb`, and the Python wheel successfully; the added
`--expected-bucket-owner` is a no-op for the correctly-owned bucket and the
surrounding prose / install steps are unchanged.

3.5 WHEN a reader adapts the updated `s3.rst` Message Broker Config Sample by
substituting their own bucket name for the `<your-bucket-name>` placeholder THEN
the system SHALL CONTINUE TO produce a valid message-broker configuration with
the same structure (targets, pipes, `s3_message_options` keys) as before.

3.6 WHEN the notebook runs with `user_provided_bucket` set to the AWS sample
bucket (preserving the original testing behavior) OR to the user's own bucket
THEN the system SHALL CONTINUE TO rewrite the segmentation manifest entries
correctly via `update_manifest_paths` and upload the updated manifest to
`s3://{bucket}/{project}/manifests/...`, with the manifest-rewrite logic and all
other notebook cells unchanged.

3.7 WHEN the review's out-of-scope items are considered — the vendored /
generated duplicate `src/backend/edgemlsdk/edgemlsdk/...` subtree (which
contains its own copies of `deploy.py`, the message-broker tests, and other
predictable-bucket references); the `cdk.out` build artifacts; the AWS-managed
buckets that are deliberately NOT renamed (`panorama-sdk-v2-artifacts`,
`lookoutvision-*`); and the already-remediated batches
(`security-injection-deserialization-fixes` for #1–#8,
`security-secrets-credentials-jwt-fixes` for S1–S9 including the
`# nosec B105` secret-name and `shlex.quote` hardening in `deploy.py`, and
`security-iam-authorization-fixes` for I1–I17) — THEN this spec SHALL CONTINUE
TO leave them unchanged.
