# Implementation Plan

## Overview

This plan follows the bug-condition methodology. Any S3 access in an in-scope
file (an `aws s3 cp` / `aws s3 sync` call, a message-broker config `bucket`
value, or a notebook download prefix) that targets a hardcoded, predictable
bucket literal (`panorama-sdk-v2-artifacts`, `edgeml-sdk-docs`,
`edgeml-sdk-longevity-tests`, `lookoutvision-*`) and is a read-then-install /
build-output-write / copy-pasteable example **without** an owner assertion /
integrity check / env-var parameterization / placeholder is the bug
(`isBugCondition(X)` true). Per the user's **Option A** decision the fix is
owner-assertion / integrity + parameterization, **NOT** a rename of the
AWS-managed buckets DDA does not own. Because the high-level `aws s3 cp` /
`aws s3 sync` commands **reject** `--expected-bucket-owner` (verified against AWS
CLI v2.35.19 in design — appending it would break every access and destroy
preservation), the resolved mechanism for every executable call site (B1, B2,
B3) is an **`aws s3api head-bucket --bucket <name> --expected-bucket-owner
<ACCOUNT>` preflight** emitted immediately before the download/upload group: it
returns `200` (no-op) on the happy path and `403` (fail-closed) on an owner
mismatch, so the existing `cp`/`sync` strings stay byte-for-byte identical.
Team-owned write buckets are additionally parameterized via env vars defaulting
to the current literals; docs / notebook references become placeholders or carry
an ownership-verification note — while preserving behavior for every legitimate
flow against a correctly-owned bucket byte-for-byte (`F(X) = F'(X)`).

- **Property 1: Fix Checking** — for all inputs where `isBugCondition` is true,
  every in-scope executable access is preceded by a `head-bucket
  --expected-bucket-owner` preflight (B1, B2, B3), team-owned write buckets read
  from an env var defaulting to the current literal (B2, B3), doc / notebook
  references are placeholders or owner-noted (B4, B5, B6), and the repo audit
  returns zero disallowed hits (Requirements 2.1–2.7).
- **Property 2: Preservation** — for all inputs where `isBugCondition` is false,
  `F(X) = F'(X)` — every legitimate download / upload / example against a
  correctly-owned bucket succeeds identically (the preflight is a `200` no-op),
  the `deploy.py` SSM strings stay byte-for-byte identical to the sibling secrets
  baseline except for the added preflight entries (with the `shlex.quote`'d args
  and `# nosec B105` line preserved), `publish.sh` with env vars unset resolves
  to the current literals, and the docs / notebook structure plus the entire
  vendored `src/backend/edgemlsdk/edgemlsdk/…` duplicate and `cdk.out` are
  unchanged (Requirements 3.1–3.7).

Finding traceability to the scan (real source paths only; the vendored
`src/backend/edgemlsdk/edgemlsdk/…` duplicate and `cdk.out/**` are generated /
vendored and out of scope):

- **B1** `src/edgemlsdk/src/test/longevity/deploy.py:~157` — `download_edgemlsdk_release_artifacts` SSM list `aws s3 sync s3://panorama-sdk-v2-artifacts/release/…` + three `aws s3 cp/sync s3://edgeml-sdk-longevity-tests/…` entries, then `dpkg -i` / `pip install`, with no owner assertion (read → install)
- **B2** `src/edgemlsdk/src/utilities/publish.sh:~23` — four `aws s3 cp *.deb/*.whl s3://panorama-sdk-v2-artifacts/release/…` uploads (versioned + `latest`) with no env var / owner assertion (build-output write)
- **B3** `src/edgemlsdk/src/utilities/publish.sh:~31` — `aws s3 sync ./sphinx s3://edgeml-sdk-docs/edgeml-sdk/v1/$major_minor/` docs upload with no env var / owner assertion (build-output write)
- **B4** `src/edgemlsdk/src/docs/source/index.rst:~42` — documented `aws s3 cp s3://panorama-sdk-v2-artifacts/dependencies/…/*.deb ./` + release downloads followed by `dpkg -i` / `pip install`, copy-pasteable with no ownership note (documented read → install)
- **B5** `src/edgemlsdk/src/docs/source/components/message_broker/s3.rst:~87` — `"bucket": "panorama-sdk-v2-artifacts"` message-broker config sample, a real guessable literal with no placeholder (copy-pasteable example)
- **B6** `DDA_SageMaker_Model_Training_and_Compilation.ipynb:~141` — `old_prefix = 's3://lookoutvision-us-east-1-0e205be246/getting-started/'` hardcoded AWS L4V sample-bucket prefix with no parameterization / prerequisite note (read prefix)
- **B7** repo-audit gate (Req 2.7) — `test/backend-test/security/s3_squat_audit.py`

## Tasks

- [x] 1. Write bug-condition exploration test (S3-squatting audit + targeted B1–B6 counterexample inspections)
  - **Property 1: Bug Condition** - An S3 access in an in-scope file targets a hardcoded, predictable bucket literal (`panorama-sdk-v2-artifacts`, `edgeml-sdk-docs`, `edgeml-sdk-longevity-tests`, `lookoutvision-*`) on a read-then-install / build-output-write / copy-pasteable-example path with NO adjacent `--expected-bucket-owner` / `head-bucket` preflight, NO env-var parameterization (team-owned writes), and NO placeholder / ownership note (docs / notebook) — across six in-scope sites (B1–B6)
  - **CRITICAL**: This test MUST FAIL (surface non-empty hits / observe the unverified access at each site) on the unfixed tree - the hits ARE the counterexamples that confirm the bug exists
  - **DO NOT attempt to fix any source / doc / notebook in this task** - this task only writes tests and documents the counterexamples
  - **NOTE**: This same audit + targeted inspection set becomes the fix-checking assertion in task 7 (it must return zero disallowed hits / observe the owner assertion / placeholder at every site after the fix)
  - **GOAL**: Enumerate every bug-condition site and demonstrate each predictable-bucket / no-owner-assertion access so the fix scope is grounded in real code
  - **Scoped PBT Approach**: the audit is deterministic (scope it to a concrete, reproducible grep over the six in-scope files); the targeted counterexample tests are concrete per-finding assertions (B1–B6); the CLI-support characterization test is a scoped observation that skips gracefully when the AWS CLI is absent
  - **Companion audit module (Req 2.7 / B7)** — create `test/backend-test/security/s3_squat_audit.py` mirroring the sibling `iam_audit.py` / `secrets_audit.py` / `repo_audit.py` two-layer shape:
    - Import the proven low-level helpers from the sibling modules where sensible — `REPO_ROOT`, `EXCLUDE_DIRS`, `EXCLUDED_PATH_SUBSTRING`, `Hit`, `_grep`, `_parse_line`, `_is_comment_line`, `_has_nosem` (with the same try/except fallback re-implementation `iam_audit.py` uses when `repo_audit` is not importable)
    - Define this spec's OWN `PREDICTABLE_BUCKETS = ("panorama-sdk-v2-artifacts", "edgeml-sdk-docs", "edgeml-sdk-longevity-tests", "lookoutvision-")`, `AUDIT_PATTERNS`, `IN_SCOPE_FILES`, and a precise `_is_disallowed`
    - `IN_SCOPE_FILES` (relative to `REPO_ROOT`) — the six real source paths this spec owns: `src/edgemlsdk/src/test/longevity/deploy.py` (B1), `src/edgemlsdk/src/utilities/publish.sh` (B2, B3), `src/edgemlsdk/src/docs/source/index.rst` (B4), `src/edgemlsdk/src/docs/source/components/message_broker/s3.rst` (B5), `DDA_SageMaker_Model_Training_and_Compilation.ipynb` (B6)
    - RAW enumeration `AUDIT_PATTERNS` (line-based, deliberately broad): `s3_cli_predictable_bucket` (`aws\s+s3\s+(?:cp|sync)\b.*(?:panorama-sdk-v2-artifacts|edgeml-sdk-docs|edgeml-sdk-longevity-tests|lookoutvision-)`); `s3_uri_predictable_bucket` (`s3://(?:panorama-sdk-v2-artifacts|edgeml-sdk-docs|edgeml-sdk-longevity-tests|lookoutvision-)`); `config_predictable_bucket` (`"bucket"\s*:\s*"(?:panorama-sdk-v2-artifacts|edgeml-sdk-docs|edgeml-sdk-longevity-tests|lookoutvision-)`); `owner_assertion` (`--expected-bucket-owner|head-bucket`, used to CLEAR a nearby access); `placeholder` (`<your-bucket-name>|<PANORAMA_SDK_ACCOUNT>|sample_data_bucket`, used to CLEAR a doc/config reference)
    - Precise gate semantics (`disallowed_hits()`; a hit is disallowed only when it is in `IN_SCOPE_FILES`, is not a comment-only line, carries no `# nosec`/`// nosec`, and matches the rule) implementing the design's three rules: `unverified_s3_access` (an `aws s3 cp`/`sync` against a predictable literal, or an `s3://<predictable>` download URI, whose **same logical block** — the preceding list entry / statement for shell/SSM lists — has no `--expected-bucket-owner` / `head-bucket` preflight); `unverified_config_reference` (a `"bucket": "<predictable>"` value or a notebook prefix bound to a bare predictable literal, not a `<…>` placeholder and not derived from a documented `sample_data_bucket` variable); `undocumented_doc_command` (an `index.rst` `aws s3 cp` example against a predictable bucket in a `code-block` with no preceding ownership `.. note::` and no documented `head-bucket` preflight in the same block)
    - **Per-bucket preflight-association semantics** — the gate parses `deploy.py`'s SSM list and `publish.sh` structurally enough to associate a `head-bucket` preflight with the access it guards (nearest preceding preflight for the **same** bucket in the same list/script), NOT a file-global presence check, so dropping the preflight for one bucket while keeping another's still fails the gate
    - Two-layer API: `run_audit()` (raw broad enumeration, no scoping/exception handling, NON-EMPTY on the unfixed tree, used by the exploration test) and `disallowed_hits()` (precise post-fix gate with `IN_SCOPE_FILES` scoping + `_has_nosem` / placeholder / preflight-association exception handling, returns `[]` after the fix)
    - Scope exclusions: the vendored `src/backend/edgemlsdk/edgemlsdk/…` duplicate (defensively via an `EXCLUDED_PATH_SUBSTRING`-style check for `os.path.join("edgemlsdk", "edgemlsdk")`), `cdk.out/**` (inherited `EXCLUDED_PATH_SUBSTRING`), and the security test files' own pattern strings
  - **Targeted exploration tests** — create `test/backend-test/security/test_s3_squat_bug_condition_exploration.py`:
    - **B1**: parse the `download_edgemlsdk_release_artifacts` SSM list in `deploy.py` (import `main`'s builder or `ast`-inspect the module) and assert on unfixed code that the `aws s3 sync s3://panorama-sdk-v2-artifacts/release/…` entry and the three `aws s3 cp/sync s3://edgeml-sdk-longevity-tests/…` entries have NO preceding `aws s3api head-bucket --expected-bucket-owner` entry for their bucket (counterexample); assert the downstream `dpkg -i` / `pip install` entries exist
    - **B2**: `bash`-aware / regex-parse `publish.sh:~23` and assert the four `.deb`/`.whl` `aws s3 cp … s3://panorama-sdk-v2-artifacts/release/…` uploads have no `ARTIFACT_BUCKET` env-var indirection and no `head-bucket` preflight (counterexample)
    - **B3**: assert `publish.sh:~31`'s `aws s3 sync ./sphinx s3://edgeml-sdk-docs/edgeml-sdk/v1/…` has no `DOCS_BUCKET` indirection and no preflight inside the `if [ -d "./sphinx" ]` guard (counterexample)
    - **B4**: parse `index.rst` and assert the dependency `aws s3 cp s3://panorama-sdk-v2-artifacts/dependencies/…` `code-block`s and the `PanoramaSDK.deb`/`.whl` release-download blocks have NO preceding ownership `.. note::` and NO documented `head-bucket` preflight (counterexample)
    - **B5**: parse `s3.rst` and assert the message-broker config sample's `"bucket"` value is the literal `"panorama-sdk-v2-artifacts"` (not a `<…>` placeholder) (counterexample)
    - **B6**: `nbformat.read` / `json.load` the notebook and assert the `seg_manifest` cell sets `old_prefix = 's3://lookoutvision-us-east-1-0e205be246/getting-started/'` as a bare literal with no `sample_data_bucket` variable and no preceding prerequisite markdown cell (counterexample)
    - **`test_s3_squat_audit_returns_no_disallowed_hits`**: assert `s3_squat_audit.disallowed_hits() == []` — this MUST FAIL on the unfixed tree (it becomes the green gate after the fix)
    - **CLI-support characterization test**: assert (skipping gracefully via `shutil.which("aws")` when the AWS CLI is absent) that `aws s3 cp … --expected-bucket-owner` and `aws s3 sync … --expected-bucket-owner` return `ParamValidation: Unknown options` while `aws s3api head-bucket … --expected-bucket-owner` accepts the flag — documenting the root cause that drives the head-bucket-preflight mechanism
  - Run the audit and targeted tests on the UNFIXED tree
  - **EXPECTED OUTCOME**: `run_audit()` returns NON-EMPTY hits across every category (B1–B6), every targeted test surfaces its counterexample, and `disallowed_hits()` is NON-EMPTY / `test_s3_squat_audit_returns_no_disallowed_hits` FAILS - this is correct, it proves the bug exists
  - Document the counterexamples found per finding (e.g. `deploy.py` SSM list has `aws s3 sync s3://panorama-sdk-v2-artifacts/release/…` with no `head-bucket` preflight; `publish.sh` uploads `.deb`/`.whl` to `panorama-sdk-v2-artifacts` with no env var / preflight; `index.rst` documents `aws s3 cp s3://panorama-sdk-v2-artifacts/dependencies/…` with no note; `s3.rst` config `bucket` == `panorama-sdk-v2-artifacts`; notebook `old_prefix` == `s3://lookoutvision-us-east-1-0e205be246/getting-started/`)
  - Mark task complete when the audit + targeted tests are written, run, and the counterexamples are documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_


- [x] 2. Write preservation baseline tests on the UNFIXED code (BEFORE implementing any fix)
  - **Property 2: Preservation** - No behavior change for legitimate (non-bug-condition) inputs
  - **IMPORTANT**: Follow observation-first methodology - capture `F(X)` baselines on the UNFIXED tree, then (in task 8) assert the fixed code `F'(X)` matches exactly
  - **Emphasize property-based tests** (Hypothesis, already vendored under `.hypothesis/`) wherever the input domain is generatable — owner-match vs owner-mismatch accounts, env-var set vs unset, notebook manifest-rewrite inputs; place the tests under `test/backend-test/security/preservation/` as `test_preservation_s3_*` so they run under the shared `security/preservation` suite already wired into the Group-1 gate
  - Observe and record baselines on unfixed code:
    - **deploy.py SSM-list golden (B1)** — import `main`'s builder (or `ast`-extract the `download_edgemlsdk_release_artifacts` list literal) for a fixed set of legitimate args and record the exact ordered list of command strings — in particular the four `aws s3 cp`/`aws s3 sync` strings, the `export AWS_DEFAULT_REGION=…`, the `mkdir`s, and the `ecr get-login-password`/`docker pull` entries — as the **sibling secrets baseline**; capture to `test/backend-test/security/baselines/s3_baseline_deploy_ssm_list.json`. Also record the exact bytes of the `# nosec B105` secret-name line and the `shlex.quote`'d argument interpolation (Req 3.1)
    - **publish.sh default-bucket resolution + upload targets golden (B2, B3)** — with `ARTIFACT_BUCKET` / `DOCS_BUCKET` unset, record that the resolved bucket literals are `panorama-sdk-v2-artifacts` / `edgeml-sdk-docs`, the versioned + `latest` `.deb`/`.whl` upload key layout, the `edgeml-sdk/v1/$major_minor/` docs-sync path, and the `if [ -d "./sphinx" ]` guard; capture as `test/backend-test/security/baselines/s3_baseline_publish_targets.json` (Req 3.2, 3.3)
    - **index.rst / s3.rst prose + structure golden (B4, B5)** — record the exact bytes of `index.rst`'s installation prose, the four `dpkg -i` steps, the `pip install` step, and the `:caption:` directives / toctree; and `s3.rst`'s target-parameter descriptions, the `"region"`/`"key"`/`"overwrite"` config keys, and the `literalinclude` samples; capture as `s3_baseline_index_rst.txt` / `s3_baseline_s3_rst.txt` (Req 3.4, 3.5)
    - **notebook golden (B6)** — record `update_manifest_paths`'s logic, the `wget` of the GitHub manifest, the upload/cleanup steps, and the current `old_prefix` string; assert `nbformat.read` / `json.load` validity; capture as `s3_baseline_notebook.json` (Req 3.6)
    - **out-of-scope guard (Req 3.7)** — record the exact bytes of the vendored `src/backend/edgemlsdk/edgemlsdk/…` duplicate (its `deploy.py`, `publish.sh`, `index.rst`, `s3.rst`, message-broker samples), `cdk.out/**`, and the files owned by the sibling remediation batches (`security-injection-deserialization-fixes` #1–#8, `security-secrets-credentials-jwt-fixes` S1–S9 incl. the `# nosec B105` / `shlex.quote` hardening, `security-iam-authorization-fixes` I1–I17) so task 8 can assert they are unchanged
  - Write tests that assert the recorded baselines. Use **property-based tests** where the domain is generatable (per the design's Testing Strategy):
    - **PBT 1 — owner matches vs owner mismatch (B1, B2, B3)**: model the preflight as `preflight(bucket_owner, expected_owner) -> pass | fail_closed`; generate `(bucket_owner, expected_owner)` account-ID pairs; invariant on the UNFIXED tree — there is NO preflight, so every access proceeds regardless of owner (the pre-fix behavior); record that legitimate (matching-owner) accesses proceed so task 8 can assert they still proceed identically (no-op preflight) while mismatched-owner accesses are the intended fail-closed DIFFERENCE
    - **PBT 2 — env var set vs unset (B2, B3)**: model bucket resolution as `resolve(env_value) -> env_value or default_literal`; generate `env_value ∈ {unset, "panorama-sdk-v2-artifacts", "edgeml-sdk-docs", "my-account-bucket", random}`; invariant on the UNFIXED tree — the targets are always the hardcoded literals; record the unset-case targets so task 8 can assert the fixed script resolves to the same literals byte-for-byte when the env vars are unset
    - **PBT 3 — deploy.py SSM list equality (B1)**: model the SSM list builder as a pure function of `(args)`; generate legitimate `args` (allowlisted `region`/`platform`/`ubuntu_version`/…); invariant on the UNFIXED tree — record the exact emitted list so task 8 can assert the fixed list equals the secrets baseline PLUS exactly the two `aws s3api head-bucket …` entries with every pre-existing entry byte-for-byte identical
    - **PBT 4 — notebook manifest rewrite unchanged (B6)**: model `update_manifest_paths(entries, old_prefix, new_prefix)`; generate manifest entries whose `source-ref`/`anomaly-mask-ref` values do / don't start with the prefix; invariant on the UNFIXED tree — record the rewrite output so task 8 can assert the fixed cell (with `old_prefix` derived from `sample_data_bucket`) computes the exact same `old_prefix` and produces identical output for every input
  - Run the tests on the UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this captures the baseline behavior to preserve — the deploy.py SSM golden, the publish.sh targets, the docs prose / structure, the notebook logic, and the owner-match / env-unset / SSM-list / notebook-rewrite invariants)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_


- [x] 3. Wave 1 — Docs + notebook FIRST (B4, B5, B6) — documentation / notebook only, zero runtime blast radius
  - **Property 1: Fix Checking** - The two Sphinx docs carry an ownership note / placeholder and the notebook parameterizes its prefix; all surrounding prose / config structure / notebook logic is byte-for-byte identical and the notebook JSON stays valid

  - [x] 3.1 B4 — `src/edgemlsdk/src/docs/source/index.rst` install instructions (~line 42)
    - Add a preceding `.. note::` stating the commands pull from the AWS-managed Panorama SDK distribution bucket and that users should verify bucket ownership / artifact integrity before `dpkg -i` / `pip install`
    - Add a documented `aws s3api head-bucket --bucket panorama-sdk-v2-artifacts --expected-bucket-owner <PANORAMA_SDK_ACCOUNT>` preflight line shown BEFORE the dependency `aws s3 cp` block and the `PanoramaSDK.deb` / `panorama-1.0-py3-none-any.whl` release-download block (do NOT append `--expected-bucket-owner` to the `aws s3 cp` lines themselves — that flag is rejected by the high-level command and would teach a broken invocation)
    - Preserve the installation prose, the four `dpkg -i` steps, the `pip install` step, the `:caption:` directives, and the toctree byte-for-byte
    - _Bug_Condition: isBugCondition(X) where X = documented `aws s3 cp s3://panorama-sdk-v2-artifacts/dependencies/…/*.deb ./` + release downloads in a `code-block` with no ownership note / preflight (B4)_
    - _Expected_Behavior: placeholderOrOwnerNoted(result) — an ownership-verification `.. note::` precedes the block and a documented `aws s3api head-bucket --expected-bucket-owner <PANORAMA_SDK_ACCOUNT>` preflight is shown before the `cp` commands (Req 2.4)_
    - _Preservation: `index.rst`'s installation prose, the four `dpkg -i` steps, the `pip install` step, the `code-block` captions, and the toctree are byte-for-byte identical; only the note / preflight line are added (Req 3.4)_
    - _Requirements: 2.4_

  - [x] 3.2 B5 — `src/edgemlsdk/src/docs/source/components/message_broker/s3.rst` config sample (~line 87)
    - Replace the message-broker config sample's `"bucket": "panorama-sdk-v2-artifacts"` value with the obvious placeholder `"bucket": "<your-bucket-name>"`
    - Add a prerequisite `.. note::` instructing users to create / own the bucket they publish to before using the config
    - Preserve the rest of the sample structure byte-for-byte — the `"region"` / `"key"` / `"overwrite"` keys, the target-parameter descriptions, the C++/Python `literalinclude` samples, and every other section
    - _Bug_Condition: isBugCondition(X) where X = message-broker config sample with `"bucket": "panorama-sdk-v2-artifacts"` (a real guessable literal, no placeholder) (B5)_
    - _Expected_Behavior: placeholderOrOwnerNoted(result) — the config `bucket` value is `<your-bucket-name>` and a prerequisite `.. note::` is present (Req 2.5)_
    - _Preservation: `s3.rst`'s `"region"`/`"key"`/`"overwrite"` keys, target-parameter descriptions, `literalinclude` samples, and every other section are byte-for-byte identical; only the `bucket` value becomes a placeholder and a `.. note::` is added (Req 3.5)_
    - _Requirements: 2.5_

  - [x] 3.3 B6 — `DDA_SageMaker_Model_Training_and_Compilation.ipynb` segmentation-manifest cell (~line 141)
    - Replace the bare `old_prefix = 's3://lookoutvision-us-east-1-0e205be246/getting-started/'` literal with a single-source `sample_data_bucket = "lookoutvision-us-east-1-0e205be246"  # AWS-owned L4V sample bucket; replace with your own` variable and `old_prefix = f's3://{sample_data_bucket}/getting-started/'`
    - Add a preceding prerequisite markdown cell (with a unique cell `id`) instructing users to create their own bucket / verify the AWS-owned sample bucket before running the cell
    - Preserve `update_manifest_paths`'s rewrite logic, the `wget` of the GitHub manifest, the upload/cleanup steps, and every other cell byte-for-byte; keep the notebook JSON valid (`nbformat` / `json.load` parseable)
    - _Bug_Condition: isBugCondition(X) where X = notebook `old_prefix` bound to the bare `lookoutvision-us-east-1-0e205be246` literal with no parameterization / prerequisite note (B6)_
    - _Expected_Behavior: placeholderOrOwnerNoted(result) — the prefix derives from a single-source `sample_data_bucket` variable preceded by a prerequisite markdown cell (Req 2.6)_
    - _Preservation: the notebook's `update_manifest_paths` logic, the manifest `wget`, the upload/cleanup steps, and every other cell are unchanged; only the `old_prefix` derivation changes and a prerequisite cell is added; the JSON remains valid (Req 3.6)_
    - _Requirements: 2.6_


- [x] 4. Wave 2 — publish.sh write path (B2, B3) — env-var parameterization + head-bucket preflight
  - **Property 1: Fix Checking** - Each upload group reads its bucket from an env var defaulting to the current literal and is preceded by an `aws s3api head-bucket --expected-bucket-owner` preflight with `|| exit 1`; the versioned + `latest` upload layout, the `edgeml-sdk/v1/$major_minor/` docs path, and the `if [ -d "./sphinx" ]` guard are preserved

  - [x] 4.1 B2 — `src/edgemlsdk/src/utilities/publish.sh` `.deb`/`.whl` uploads (~line 23)
    - Add `ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-panorama-sdk-v2-artifacts}"` (with a comment guiding users to set an account-scoped name) and `EXPECTED_BUCKET_OWNER="${EXPECTED_BUCKET_OWNER:-$(aws sts get-caller-identity --query Account --output text)}"`
    - Emit an `aws s3api head-bucket --bucket "$ARTIFACT_BUCKET" --expected-bucket-owner "$EXPECTED_BUCKET_OWNER" || exit 1` preflight BEFORE the four `.deb`/`.whl` uploads, so a squatted bucket fails closed before any build output is published
    - Change the four uploads to target `s3://${ARTIFACT_BUCKET}/release/…`, preserving the versioned + `latest` dual-upload semantics and the release-path layout byte-for-byte
    - _Bug_Condition: isBugCondition(X) where X = `aws s3 cp *.deb/*.whl s3://panorama-sdk-v2-artifacts/release/…` uploads with no env var / owner assertion (B2)_
    - _Expected_Behavior: ownerAssertedOrIntegrityVerified(result) AND parameterizedWhereTeamOwned(result) — `ARTIFACT_BUCKET` resolves the bucket (default `panorama-sdk-v2-artifacts`) and a `head-bucket --expected-bucket-owner` preflight with `|| exit 1` precedes the uploads (Req 2.2)_
    - _Preservation: run without `ARTIFACT_BUCKET` set, the bucket resolves to `panorama-sdk-v2-artifacts` and the versioned + `latest` `.deb`/`.whl` uploads land at the same release paths; the preflight is a `200` no-op against a correctly-owned bucket (Req 3.2)_
    - _Requirements: 2.2_

  - [x] 4.2 B3 — `src/edgemlsdk/src/utilities/publish.sh` docs sync (~line 31)
    - Add `DOCS_BUCKET="${DOCS_BUCKET:-edgeml-sdk-docs}"` and, INSIDE the `if [ -d "./sphinx" ]` guard, emit an `aws s3api head-bucket --bucket "$DOCS_BUCKET" --expected-bucket-owner "$EXPECTED_BUCKET_OWNER" || exit 1` preflight before the sync
    - Change the sync to target `s3://${DOCS_BUCKET}/edgeml-sdk/v1/$major_minor/`, preserving the `edgeml-sdk/v1/$major_minor/` path layout and the `if [ -d "./sphinx" ]` guard byte-for-byte
    - _Bug_Condition: isBugCondition(X) where X = `aws s3 sync ./sphinx s3://edgeml-sdk-docs/edgeml-sdk/v1/…` with no env var / owner assertion (B3)_
    - _Expected_Behavior: ownerAssertedOrIntegrityVerified(result) AND parameterizedWhereTeamOwned(result) — `DOCS_BUCKET` resolves the docs bucket (default `edgeml-sdk-docs`) and a `head-bucket --expected-bucket-owner` preflight runs inside the `if [ -d "./sphinx" ]` guard (Req 2.3)_
    - _Preservation: run without `DOCS_BUCKET` set with `./sphinx` present, the bucket resolves to `edgeml-sdk-docs` and the docs sync to the same `edgeml-sdk/v1/$major_minor/` path; the guard is preserved; the preflight is a `200` no-op against a correctly-owned bucket (Req 3.3)_
    - _Requirements: 2.3_


- [x] 5. Wave 3 — deploy.py read path (B1) LAST — highest blast radius; head-bucket preflight in the SSM list
  - **Property 1: Fix Checking** - The `download_edgemlsdk_release_artifacts` SSM list has an `aws s3api head-bucket --expected-bucket-owner` entry before the `panorama-sdk-v2-artifacts` sync and before the three `edgeml-sdk-longevity-tests` accesses, with `shlex.quote`'d owner values; the four existing `aws s3 cp`/`aws s3 sync` strings and the `# nosec B105` line are byte-for-byte identical

  - [x] 5.1 B1 — `src/edgemlsdk/src/test/longevity/deploy.py` `main(args)` SSM list (~line 157)
    - Source the two expected owner account IDs near the top of `main` (alongside the existing `session`/`aws_region` setup): `artifacts_bucket_owner = args.artifacts_bucket_owner or os.environ.get("ARTIFACTS_BUCKET_OWNER") or PANORAMA_SDK_DISTRIBUTION_ACCOUNT` (documented module constant, the AWS-managed Panorama distribution account, filled at impl time) and `longevity_bucket_owner = args.longevity_bucket_owner or os.environ.get("LONGEVITY_BUCKET_OWNER") or session.client("sts", region_name=aws_region).get_caller_identity()["Account"]` (team-owned → defaults to the deployer's caller identity, a no-op preflight for the legitimate deployer)
    - Add two argparse args mirroring the existing `--platform`/`--region` pattern: `--artifacts-bucket-owner` and `--longevity-bucket-owner`, both `type=str, default=None`, so resolution falls through to env / caller-identity / documented-constant
    - Prepend two `aws s3api head-bucket` preflight entries into the `download_edgemlsdk_release_artifacts` list using `shlex.quote` on the interpolated owner values (bucket names stay bare literals exactly as the `s3://…` literals already are): `f"aws s3api head-bucket --bucket panorama-sdk-v2-artifacts --expected-bucket-owner {shlex.quote(str(artifacts_bucket_owner))}"` immediately before the `panorama-sdk-v2-artifacts` `aws s3 sync` entry, and `f"aws s3api head-bucket --bucket edgeml-sdk-longevity-tests --expected-bucket-owner {shlex.quote(str(longevity_bucket_owner))}"` immediately before the three `edgeml-sdk-longevity-tests` `aws s3 cp`/`sync` entries (AWS-RunShellScript runs the list sequentially and aborts on a non-zero exit, so a `403` fails the batch closed before `dpkg -i` / `pip install`)
    - Preserve the four existing `aws s3 cp`/`aws s3 sync` strings, the `export AWS_DEFAULT_REGION=…`, the `mkdir`s, the `ecr get-login-password`/`docker pull` entries, the `shlex.quote`'d `q_region`/`q_platform`/… interpolation, the boto3 upload helpers, the `mqtt` branch, and the `# nosec B105` secret-name line byte-for-byte — the fix ADDS only the two `head-bucket` entries
    - _Bug_Condition: isBugCondition(X) where X = SSM `aws s3 sync s3://panorama-sdk-v2-artifacts/release/…` + three `aws s3 cp/sync s3://edgeml-sdk-longevity-tests/…` read-then-install entries with no owner assertion (B1)_
    - _Expected_Behavior: ownerAssertedOrIntegrityVerified(result) — an `aws s3api head-bucket --expected-bucket-owner` preflight precedes the `panorama-sdk-v2-artifacts` sync and the three `edgeml-sdk-longevity-tests` accesses, owner values `shlex.quote`'d from args/env (`--artifacts-bucket-owner`/`ARTIFACTS_BUCKET_OWNER` + documented Panorama constant; `--longevity-bucket-owner`/`LONGEVITY_BUCKET_OWNER` + `sts get-caller-identity`) (Req 2.1)_
    - _Preservation: F(X)=F'(X) — the emitted SSM list equals the sibling secrets baseline PLUS exactly the two `head-bucket` entries; the four `aws s3 cp`/`aws s3 sync` strings, the `# nosec B105` line, and the `shlex.quote`'d args are byte-for-byte identical; the preflight is a `200` no-op against correctly-owned buckets (Req 3.1)_
    - _Requirements: 2.1_


- [x] 6. B7 — Finalize the S3-squatting audit gate (Req 2.7)
  - **Property 1: Fix Checking** - `s3_squat_audit.disallowed_hits()` returns `[]` on the fixed tree with per-bucket preflight-association semantics, and still fails on a reintroduced unverified access
  - Finalize `test/backend-test/security/s3_squat_audit.py` (created in task 1) so `disallowed_hits()` returns `0` on the fixed tree: the `unverified_s3_access` rule clears each `deploy.py` SSM access via the nearest preceding same-bucket `head-bucket` preflight and each `publish.sh` upload group via its preflight; `unverified_config_reference` clears `s3.rst` via the `<your-bucket-name>` placeholder and the notebook via the `sample_data_bucket` variable; `undocumented_doc_command` clears `index.rst` via the ownership `.. note::` + documented preflight
    - Verify the **per-bucket preflight-association** semantics hold: dropping the `head-bucket` preflight for `panorama-sdk-v2-artifacts` while keeping the `edgeml-sdk-longevity-tests` one (or vice-versa) still produces a disallowed hit — assert this with a negative fixture so the gate cannot be satisfied by a file-global preflight presence
    - Confirm `run_audit()` still enumerates the raw literals (non-empty) but `disallowed_hits()` is `[]`, and that both exclude the vendored `src/backend/edgemlsdk/edgemlsdk/…` duplicate and `cdk.out`
    - _Bug_Condition: isBugCondition(X) where X = any predictable-bucket access in an in-scope file without an owner assertion / placeholder / note surfaced by the audit (B7 / Req 1.7)_
    - _Expected_Behavior: `s3_squat_audit.disallowed_hits() == []` after B1–B6, with per-bucket preflight-association semantics; a reintroduced unverified access re-fails the gate (Req 2.7)_
    - _Preservation: `run_audit()` / `disallowed_hits()` are scoped to `IN_SCOPE_FILES` and exclude the vendored duplicate + `cdk.out`, leaving every out-of-scope file untouched (Req 3.7)_
    - _Requirements: 2.7_


- [x] 7. Verify the bug-condition exploration test now passes (Fix Checking)
  - **Property 1: Expected Behavior** - Every in-scope S3 access is squatting-resistant: read/write sites carry a `head-bucket --expected-bucket-owner` preflight, team-owned writes read from an env var, docs / notebook references are placeholders or owner-noted, and the audit returns zero disallowed hits
  - **IMPORTANT**: Re-run the SAME audit + targeted tests from task 1 - do NOT write new tests
  - Re-run `s3_squat_audit.run_audit()` / `disallowed_hits()` and `test_s3_squat_bug_condition_exploration.py` on the FIXED tree
  - **EXPECTED OUTCOME**: every task-1 counterexample is now neutralized — the B1/B2/B3 accesses have their preflight, `index.rst` has its note + documented preflight, `s3.rst` shows `<your-bucket-name>`, the notebook derives from `sample_data_bucket`, `test_s3_squat_audit_returns_no_disallowed_hits` PASSES, and `disallowed_hits() == []` (confirms the bug is fixed)
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_


- [x] 8. Verify preservation baseline tests still pass (Preservation Checking)
  - **Property 2: Preservation** - No behavior change for legitimate inputs — `F(X) = F'(X)`
  - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
  - Re-run the `test_preservation_s3_*` suite (goldens + PBTs 1–4) on the FIXED tree
  - **EXPECTED OUTCOME**: all preservation tests PASS — the `deploy.py` SSM command strings equal the secrets baseline PLUS exactly the two `head-bucket` entries (the four `cp`/`sync` strings + `# nosec B105` byte-for-byte); `publish.sh` with `ARTIFACT_BUCKET`/`DOCS_BUCKET` unset resolves to the current literals and the same upload / docs-sync layout; `index.rst` / `s3.rst` structure and every notebook cell / `update_manifest_paths` output are preserved (valid JSON); PBT 1 (preflight no-op on owner-match, fail-closed on mismatch), PBT 2 (unset resolves to current literal), PBT 3 (SSM list == baseline + two entries), and PBT 4 (notebook rewrite unchanged) invariants hold; the vendored duplicate + `cdk.out` + sibling-spec files are byte-for-byte unchanged
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_


- [x] 9. Integration + CI-gate verification
  - [x] 9.1 Run the backend security suites and wire `s3_squat_audit.py` into `build-custom.sh`
    - Run the backend security suites to completion — `s3_squat_audit.py`, `test_s3_squat_bug_condition_exploration.py`, and the `security/preservation` suite (`test_preservation_s3_*`) — and confirm the S3-squatting unit + property tests pass with no regressions
    - Wire the new gate as a **fourth** security block in `build-custom.sh` (the "Security … audit gate" region, after the IAM gate at ~line 254–258), under the same `set -e`-guarded backend-test block so a non-zero exit fails the build:
      ```sh
      echo "Running security S3 bucket-squatting audit gate..."
      python${PYTHON_VERSION} test/backend-test/security/s3_squat_audit.py
      python${PYTHON_VERSION} -m pytest \
        test/backend-test/security/test_s3_squat_bug_condition_exploration.py -v
      echo "Security S3 bucket-squatting audit gate passed."
      ```
    - Confirm the shared `security/preservation` suite (already run by the Group-1 gate) picks up the new `test_preservation_s3_*` files — no separate wiring needed
    - Verify the gate fails the build if a predictable-bucket access without an owner assertion / placeholder / note reappears in in-scope source (negative check), and that it does NOT match the vendored duplicate or `cdk.out`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_


- [x] 10. Checkpoint - Ensure all tests pass and the CI gate is wired
  - Ensure the exploration test passes on fixed code (task 7), the preservation goldens are byte-for-byte identical and PBTs 1–4 invariants hold (task 8), and the backend security suites + integration checks pass (task 9)
  - Confirm the `s3_squat_audit.py` gate (plus the exploration and preservation suites) is wired into `build-custom.sh` so a predictable-bucket access without a `head-bucket --expected-bucket-owner` preflight / placeholder / ownership note reappearing in in-scope source fails the build
  - Confirm the deployment-time gate items — the `ARTIFACTS_BUCKET_OWNER` / `LONGEVITY_BUCKET_OWNER` (B1) and `EXPECTED_BUCKET_OWNER` (B2/B3) account values are correctly sourced per environment (a wrong value fails the deploy/publish closed at the preflight), and the deployment runbook records the correct Panorama SDK distribution account for `PANORAMA_SDK_DISTRIBUTION_ACCOUNT`
  - Ensure all tests pass; ask the user if questions arise


---

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "description": "Run on UNFIXED code: surface predictable-bucket / no-owner-assertion counterexamples (S3-squatting audit + targeted B1–B6 inspections + CLI-support characterization) and capture preservation baselines (deploy.py SSM golden, publish.sh default-bucket targets, index.rst/s3.rst prose+structure, notebook logic, out-of-scope guard; PBT 1–4 baselines) (independent).", "tasks": ["1", "2"] },
    { "wave": 2, "description": "Wave 1 fixes — Docs + notebook FIRST (B4, B5, B6): documentation / notebook only, zero runtime blast radius.", "tasks": ["3.1", "3.2", "3.3"] },
    { "wave": 3, "description": "Wave 2 fixes — publish.sh write path (B2, B3): env-var parameterization + head-bucket preflight in a build/release script; impact radius is one publish run.", "tasks": ["4.1", "4.2"] },
    { "wave": 4, "description": "Wave 3 fix LAST — deploy.py read path (B1): head-bucket preflight prepended to the SSM download list; highest blast radius (governs what a deployed EC2 instance dpkg -i / pip installs). Land last so a wrong owner value cannot silently break a deploy before the gate is green.", "tasks": ["5.1"] },
    { "wave": 5, "description": "B7 — finalize the s3_squat_audit.py gate: disallowed_hits() == 0 on the fixed tree with per-bucket preflight-association semantics; still fails on a reintroduced unverified access.", "tasks": ["6"] },
    { "wave": 6, "description": "Fix Checking and Preservation Checking (re-run tasks 1 and 2 on fixed code, including PBTs 1–4).", "tasks": ["7", "8"] },
    { "wave": 7, "description": "Integration + CI-gate verification (backend security suites + wire s3_squat_audit into build-custom.sh as the 4th gate; shared preservation suite already runs test_preservation_s3_*).", "tasks": ["9.1"] },
    { "wave": 8, "description": "Checkpoint: all green + CI gate wired + deployment-time owner-account gate items confirmed.", "tasks": ["10"] }
  ]
}
```

Visual summary of the critical path:

```
1. Bug-condition audit + targeted tests (FAILS: predictable-bucket accesses with no preflight/placeholder/note across B1–B6, disallowed_hits() non-empty)
2. Preservation baselines (PASS on unfixed tree: deploy.py SSM golden, publish.sh default-bucket targets, index.rst/s3.rst prose+structure, notebook logic, PBT 1–4 baselines)
        │  (1 and 2 are independent; both run on UNFIXED code first)
        ▼
2. WAVE 1 — DOCS + NOTEBOOK FIRST (documentation / notebook only, zero runtime blast radius)
   3.1 B4 index.rst → ownership .. note:: + documented head-bucket --expected-bucket-owner preflight (prose/dpkg/pip preserved)
   3.2 B5 s3.rst  → "bucket": "<your-bucket-name>" placeholder + prerequisite .. note:: (config structure preserved)
   3.3 B6 notebook → sample_data_bucket variable + prerequisite markdown cell (update_manifest_paths + JSON validity preserved)
        │
        ▼
3. WAVE 2 — publish.sh WRITE PATH (build/release script, one publish run per invocation)
   4.1 B2 publish.sh .deb/.whl → ARTIFACT_BUCKET env var + head-bucket --expected-bucket-owner preflight || exit 1 (versioned+latest layout preserved)
   4.2 B3 publish.sh docs sync → DOCS_BUCKET env var + head-bucket preflight inside the if [ -d "./sphinx" ] guard (edgeml-sdk/v1/$major_minor/ path preserved)
        │
        ▼
4. WAVE 3 — deploy.py READ PATH (LAST — highest blast radius, governs deployed-instance installs)
   5.1 B1 deploy.py SSM list → two aws s3api head-bucket --expected-bucket-owner entries prepended (shlex.quote'd owner values from args/env),
        the four aws s3 cp/sync strings + # nosec B105 byte-for-byte unchanged
        │                                    ── deployment-time gate: source the correct owner accounts!
        ▼
5. B7 AUDIT GATE FINALIZE
   6. s3_squat_audit.disallowed_hits() == 0 (per-bucket preflight-association; still fails on a reintroduced unverified access)
        │
        ├──────────────┐
        ▼              ▼
6. 7. Fix Checking    8. Preservation Checking
    (re-run task 1:    (re-run task 2:
     preflight/         F(X) = F'(X),
     placeholder/note   SSM list == baseline
     at every site,     + 2 preflight entries,
     disallowed_hits    env-unset → literals,
     == 0, PBTs)        docs/notebook preserved,
                        PBTs 1–4 hold)
        │              │
        └──────┬───────┘
               ▼
7. 9.1 Integration + CI-gate verification (security suites + wire build-custom.sh 4th gate + preservation suite runs test_preservation_s3_*)
               │
               ▼
8. 10. Checkpoint (all green + CI audit guard wired + deployment-time owner-account gate items confirmed)
```
