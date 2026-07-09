# Implementation Plan

## Overview

This plan follows the bug-condition methodology. Any application-code path that
carries a secret value (bearer token, AWS access/secret key) into a log / command
sink, exposes a real-looking credential in copy-pasteable text, or presents a
scanner-flagged pattern with no documented exception is the bug
(`isBugCondition(X)` true). The fix **neutralizes** every one of the nine sites —
the sink is closed / redacted, the credential fragment is removed in favour of the
EC2 IAM instance profile, the copy-pasteable literal becomes an obvious
placeholder, or the intentional pattern carries a documented `# nosem`/`# nosec` +
rationale — while preserving behavior for every legitimate input byte-for-byte
(`F(X) = F'(X)`).

- **Property 1: Fix Checking** — for all inputs where `isBugCondition` is true,
  the fixed code neutralizes the sink / documents the pattern (the authorizer log
  never contains a token; the SSM commands contain no key; the email/password are
  placeholders; each flagged line carries a documented exception) and the repo
  audit returns zero disallowed hits (Requirements 2.1–2.10).
- **Property 2: Preservation** — for all inputs where `isBugCondition` is false,
  `F(X) = F'(X)` (Requirements 3.1–3.10).

Finding traceability to the scan (real source paths only; the
`edge-cv-portal/infrastructure/cdk.out/asset.*` copies and the vendored
`src/backend/edgemlsdk/edgemlsdk/` duplicate of `deploy.py` are generated /
vendored and out of scope):

- **S1** `edge-cv-portal/backend/functions/jwt_authorizer.py:272` — full-event log (bearer token → CloudWatch Logs)
- **S2** `src/edgemlsdk/src/test/longevity/deploy.py:~230–231, ~250` — AWS keys → `AWS-RunShellScript` SSM command strings
- **S3** `edge-cv-portal/backend/functions/packaging.py:~440` — real registrable domain `system@edgecv.com`
- **S4** `README.md:~208` — real-looking password `YourSecurePassword1234!`
- **S5** `edge-cv-portal/backend/functions/jwt_authorizer.py:131` — intentional unverified JWT pre-parse (no `# nosem`)
- **S6** `src/backend/app.py:271, 274` — intentional `0.0.0.0` LAN-UI bind (no `# nosem`/`# nosec`)
- **S7** `edge-cv-portal/backend/functions/components.py:305` — B105 `pagination_token = ''` (empty cursor, not a secret)
- **S8** `src/edgemlsdk/src/test/longevity/deploy.py:~59, ~207` — B105 bucket/secret name `'edgeml-sdk-longevity-tests'`
- **S9** `test/backend-test/utils/test_auth.py:47, 60, 71, 83, 95, 106` — B106 test-only token literals
- **S10** repo-audit gate (Req 2.10) — `secrets_audit.py`

## Tasks

- [ ] 1. Write bug-condition exploration test (secrets/credentials/JWT audit + targeted exploit-shaped tests)
  - **Property 1: Bug Condition** - A secret value reaches a log / command sink, a real-looking credential sits in copy-pasteable text, or a scanner-flagged pattern lacks a documented exception across nine application-code sites
  - **CRITICAL**: This test MUST FAIL (surface non-empty hits / observe the secret at the sink) on the unfixed tree - the hits ARE the counterexamples that confirm the bug exists
  - **DO NOT attempt to fix any application source code in this task** - this task only writes tests and documents the counterexamples
  - **NOTE**: This same audit + exploit set becomes the fix-checking assertion in task 7 (it must return zero disallowed hits / observe no secret at any sink after the fix)
  - **GOAL**: Enumerate every bug-condition site and demonstrate each sink so the fix scope is grounded in real code
  - **Scoped PBT Approach**: the audit is deterministic (scope it to a concrete, reproducible grep over the known in-scope tree); the targeted tests use Hypothesis (already vendored under `.hypothesis/`) where the input domain is generatable (authorizer events carrying secret-bearing tokens/headers, deploy args), scoped to concrete failing shapes for reproducibility
  - **Companion audit module (Req 2.10 / S10)** — create `test/backend-test/security/secrets_audit.py` mirroring the sibling `repo_audit.py`'s two-layer shape:
    - Import the proven low-level helpers from the sibling module where sensible — `REPO_ROOT`, `EXCLUDE_DIRS`, `EXCLUDED_PATH_SUBSTRING`, `Hit`, `_grep`, `_parse_line`, `_has_nosem`, `_is_comment_line`, `_is_in_scope` (or a thin re-implementation if import coupling is undesirable)
    - Define this spec's OWN `AUDIT_PATTERNS`, `IN_SCOPE_FILES`, and a precise `_is_disallowed` for the three sinks, scoped to `IN_SCOPE_FILES` = `edge-cv-portal/backend/functions/jwt_authorizer.py`, `src/edgemlsdk/src/test/longevity/deploy.py`, `edge-cv-portal/backend/functions/packaging.py`, `src/backend/app.py`, `edge-cv-portal/backend/functions/components.py`, `test/backend-test/utils/test_auth.py`
    - `log_event_dump` — logging call whose argument contains `json.dumps(event`: `logger\.(info|debug|warning|error|critical)\(.*json\.dumps\(\s*event\b`; disallowed when present without a documented marker
    - `cred_in_command` — `\.(access_key|secret_key)\b` (or `{...access_key...}`/`{...secret_key...}`) occurring inside a string literal being built for a command — an f-string (`\b[rRbB]?f['\"]`) / `%` / `.format(` / `+` interpolation; the bare boto3 kwargs `aws_access_key_id=credentials.access_key` do NOT match and are allowed
    - `unverified_jwt` — `verify_signature['\"]?\s*[:=]\s*False` WITHOUT a documented `# nosem` on the line
    - (Enumeration-only) undocumented `0.0.0.0` bind (`host\s*=\s*['\"]0\.0\.0\.0['\"]` without `# nosem`/`# nosec`) and B105/B106 literals — surfaced by the raw `run_audit()` enumeration but treated as documented by the precise gate once S6–S9 land
    - Provide a raw `run_audit()` (broad enumeration, non-empty on unfixed tree, used by the exploration test) and a precise `disallowed_hits()` (zero after fix, minus documented exceptions)
    - Scope exclusions: `cdk.out/asset.*`, the vendored `src/backend/edgemlsdk/edgemlsdk/` duplicate of `deploy.py`, and the security test files' own pattern strings
  - **Targeted exploit-shaped tests** — create `test/backend-test/security/test_secrets_bug_condition_exploration.py`:
    - **S1**: call `handler(event, ctx)` with an event carrying `authorizationToken`/`headers` bearer tokens; capture the log record and assert the emitted log line **contains** the token substring on unfixed code (counterexample)
    - **S2**: build the command lists with a stubbed `credentials` (fake access/secret keys) and assert the fake access key AND secret key **appear** verbatim in the constructed `download_edgemlsdk_release_artifacts` and `run_mqtt_longevity` command strings on unfixed code (counterexample)
    - **S5**: assert the `verify_signature: False` pre-parse line (line 131) carries **no** `# nosem` on unfixed code (audit hit)
  - Run the audit and targeted tests on the UNFIXED tree
  - **EXPECTED OUTCOME**: `run_audit()` returns NON-EMPTY hits across S1/S2/S5 (and enumerates the S6–S9 undocumented markers) AND each targeted test surfaces its counterexample (token in the S1 log line; fake keys in the S2 command strings; no `# nosem` on the S5 line) - this is correct, it proves the bug exists
  - Document the counterexamples found per finding (e.g. `handler(event)` log line contains `Bearer eyJ...`; `deploy.py` command string contains `export AWS_ACCESS_KEY_ID=AKIA...` and `-a AKIA... -s <secret>`; line 131 has no documented marker)
  - Mark task complete when the audit + targeted tests are written, run, and the counterexamples are documented
  - _Requirements: 1.1, 1.2, 1.5, 1.10_

- [ ] 2. Write preservation baseline tests on the UNFIXED code (BEFORE implementing any fix)
  - **Property 2: Preservation** - No behavior change for legitimate (non-bug-condition) inputs
  - **IMPORTANT**: Follow observation-first methodology - capture `F(X)` baselines on the UNFIXED tree, then (in task 8) assert the fixed code `F'(X)` matches exactly
  - **Emphasize property-based tests** (Hypothesis, already vendored under `.hypothesis/`) wherever the input domain is generatable; place the tests under `test/backend-test/security/preservation/` alongside the sibling suite
  - Observe and record baselines on unfixed code:
    - **S1** `jwt_authorizer.py`: for a given (mocked-decode) authorizer event, record the returned allow/deny IAM policy (`principalId`, `Effect`, `Resource`, `context`) — the policy is a pure function of the token/claims, not of the log line (Req 3.1)
    - **S2** `deploy.py`: for canonical valid args (valid platform / ubuntu+python versions / region / MQTT endpoint / 8-digit release date / numeric sizes) with a stubbed `credentials`, record the EXACT constructed `download_edgemlsdk_release_artifacts` and `run_mqtt_longevity` SSM command strings — including the sibling spec's `shlex.quote`'d `-l/-r/-m/-n` fragments (Req 3.2)
    - **S3** `packaging.py`: record the synthetic-identity construction result apart from the email value (Req 3.3)
    - **S5** `jwt_authorizer.py`: record that a valid token validates to the same claims and a tampered token raises `AuthorizationError` under the two-stage decode (Req 3.5)
    - **S6** `app.py`: record the uvicorn bind config — `0.0.0.0:5443` TLS with authorization enabled, `0.0.0.0:5000` plaintext when disabled (Req 3.6)
    - **S7** `components.py`: record the empty `pagination_token` cursor initialization and `list_private_components` pagination behavior (Req 3.7)
    - **S8** `deploy.py`: record the `'edgeml-sdk-longevity-tests'` value and its S3 / Secrets Manager operations (Req 3.8)
    - **S9** `test_auth.py`: run the existing suite; record that all `auth.validate_token` cases pass with the current fixtures (Req 3.9)
    - **Out-of-scope guard**: record the exact bytes of the `cdk.out/asset.*` copies, the vendored `edgemlsdk/edgemlsdk/` duplicate, and the boto3 client kwarg lines (`aws_access_key_id=credentials.access_key`, etc.) in `deploy.py` so task 8 can assert they are unchanged (Req 3.10)
  - Write tests that assert the recorded baselines. Use **property-based tests** where the domain is generatable (per the design's Testing Strategy):
    - **S1**: generate authorizer events with random `methodArn`, random secret-bearing `authorizationToken`/`headers`, and random claims (mock `validate_jwt_token` / JWKS so the decode is deterministic); invariant — the returned IAM policy is identical to `F`'s for the same event
    - **S2**: generate valid arg tuples (platform/ubuntu/python/region/date/mqtt-endpoint shapes that pass the sibling spec's allowlist) with a stubbed `credentials`; invariant — the constructed command strings equal `F`'s baseline strings
  - Run the tests on the UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this captures the baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10_

- [ ] 3. Doc / `# nosem`-only fixes FIRST (S3, S4, S5, S6, S7, S8, S9) — zero runtime risk
  - **Property 1: Fix Checking** - Real-looking credentials neutralized and each intentional scanner-flagged pattern documented (comment / literal-text edits only, no control-flow change)

  - [ ] 3.1 S3 — `packaging.py`: RFC 2606 reserved domain
    - Change the synthetic-identity email literal (in the auto-triggered `greengrass_event` `requestContext.authorizer.claims`) from `'email': 'system@edgecv.com'` to `'email': 'system@example.com'`
    - No other value changes
    - _Bug_Condition: isBugCondition(X) where X = real registrable domain in copy-pasteable synthetic-identity text (S3)_
    - _Expected_Behavior: the email uses an RFC 2606 reserved domain (`system@example.com`)_
    - _Preservation: all other packaging behavior is byte-for-byte identical (Req 3.3)_
    - _Requirements: 2.3_

  - [ ] 3.2 S4 — `README.md`: placeholder password (doc-only)
    - In the Cognito admin setup command, replace `  --password YourSecurePassword1234! \` with `  --password <YOUR_SECURE_PASSWORD> \`
    - _Bug_Condition: isBugCondition(X) where X = real-looking password literal in a copy-pasteable doc command (S4)_
    - _Expected_Behavior: the password is an obvious, non-copyable placeholder (`<YOUR_SECURE_PASSWORD>`)_
    - _Preservation: the command flags and structure are otherwise unchanged (Req 3.4)_
    - _Requirements: 2.4_

  - [ ] 3.3 S5 — `jwt_authorizer.py`: documented unverified pre-parse (line 131)
    - Add a documented `# nosem` marker (using the exact Semgrep rule id reported for the finding) + a comment on the `jwt.decode(token, options={"verify_signature": False})` line recording that the unverified read obtains `kid`/`iss` ONLY for JWKS-key selection and that a full RS256 signature-verified decode (`verify_exp`/`verify_aud`/`verify_iss`) is enforced afterward at ~line 178 before any claim is used
    - No control-flow change; the two-stage decode is unchanged
    - Include the tampered-token-rejected assertion in the accompanying test (a tampered token is still rejected by the verified decode)
    - _Bug_Condition: isBugCondition(X) where X = intentional unverified JWT pre-parse with no documented exception (S5)_
    - _Expected_Behavior: the pre-parse line carries a documented `# nosem` + rationale noting full verification is enforced after; a tampered token is still rejected_
    - _Preservation: the two-stage decode is unchanged — a valid token still validates, a tampered token is still rejected; the annotation changes no control flow (Req 3.5)_
    - _Requirements: 2.5_

  - [ ] 3.4 S6 — `app.py`: documented LAN-UI bind (lines 271, 274)
    - Add a `# nosem`/`# nosec B104` marker + rationale on BOTH `uvicorn.Config(app, host="0.0.0.0", ...)` lines: the 5443 line noting it is TLS-protected AND gated by station authorization; the 5000 line noting it is the auth-disabled-only plaintext path, intentional for an on-device edge appliance serving the LAN UI
    - No bind behavior changes
    - _Bug_Condition: isBugCondition(X) where X = intentional `0.0.0.0` LAN-UI bind with no documented exception (S6)_
    - _Expected_Behavior: both bind lines carry a documented `# nosem`/`# nosec` + rationale_
    - _Preservation: the same host (`0.0.0.0`) and ports (5443 TLS when auth enabled, 5000 plaintext when disabled) with the same uvicorn config (Req 3.6)_
    - _Requirements: 2.6_

  - [ ] 3.5 S7 — `components.py`: B105 false positive (line 305)
    - On the `pagination_token = ''` line add `# nosec B105` + a one-line rationale, e.g. `# nosec B105 — empty pagination cursor, not a secret.`
    - Value and pagination behavior unchanged
    - _Bug_Condition: isBugCondition(X) where X = empty-string pagination cursor flagged B105 with no documented exception (S7)_
    - _Expected_Behavior: the line carries a documented `# nosec` + rationale_
    - _Preservation: the empty `pagination_token` cursor initializes and pages exactly as before (Req 3.7)_
    - _Requirements: 2.7_

  - [ ] 3.6 S8 — `deploy.py`: B105 false positive (~lines 59, 207)
    - On the `secret_name = "edgeml-sdk-longevity-tests"` line (~59) add `# nosec B105 — Secrets Manager secret name / S3 bucket name, not a password.`; apply the same annotation to the `bucket_name = 'edgeml-sdk-longevity-tests'` literal in `main` (~line 207)
    - Values and their S3 / Secrets Manager operations unchanged
    - _Bug_Condition: isBugCondition(X) where X = non-secret bucket/secret name flagged B105 with no documented exception (S8)_
    - _Expected_Behavior: both literals carry a documented `# nosec` + rationale_
    - _Preservation: the same `'edgeml-sdk-longevity-tests'` value is used for the same S3 / Secrets Manager operations (Req 3.8)_
    - _Requirements: 2.8_

  - [ ] 3.7 S9 — `test_auth.py`: B106 false positives (lines 47, 60, 71, 83, 95, 106)
    - On each of the six `token=` argument lines (the `token=""`, `token="good-token"`, `token="inactive-token"`, `token="some-token"` fixtures) add a `# nosec B106` + rationale, e.g. `# nosec B106 — test-only fixture, not a real credential.`
    - Test inputs, assertions, and outcomes unchanged; the suite still passes
    - _Bug_Condition: isBugCondition(X) where X = test-only token literals flagged B106 with no documented exception (S9)_
    - _Expected_Behavior: each of the six lines carries a documented `# nosec` + rationale_
    - _Preservation: the same scenarios with the same fixtures and assertions; all tests still pass (Req 3.9)_
    - _Requirements: 2.9_

- [ ] 4. S1 — `jwt_authorizer.py`: redact the invocation log (substantive, after the doc-only changes)
  - [ ] 4.1 Add `_safe_event_metadata` and replace the full-event log
    - **Property 1: Fix Checking** - The JWT authorizer invocation log never receives a bearer token
    - Add a `_safe_event_metadata(event)` helper near the top of the module that returns ONLY non-sensitive fields — `{"methodArn": event.get("methodArn")}` plus `requestId` when `requestContext.requestId` is present — and NEVER `authorizationToken` or `headers`
    - Replace the sink line `logger.info(f"JWT Authorizer invoked with event: {json.dumps(event, default=str)}")` (line 272) with a redacted log of the small metadata dict only, e.g. `logger.info("JWT Authorizer invoked: %s", _safe_event_metadata(event))`
    - Leave every other statement unchanged, including `logger.info(f"Authorization successful for user: {user_id}")` (non-secret subject id) and the `generate_policy(...)` allow/deny output; keep `json` imported if still referenced elsewhere
    - _Bug_Condition: isBugCondition(X) where X = full authorizer event (carrying `authorizationToken`/`headers`) reaching the CloudWatch log sink (S1)_
    - _Expected_Behavior: the invocation log line contains only `methodArn` (and `requestId` when present) and never the token, headers, or any token-bearing field_
    - _Preservation: the authorizer emits the same allow/deny IAM policy (`principalId`, `Effect`, `Resource`, `context`) for the same event; only the log-line content changes (Req 3.1)_
    - _Requirements: 2.1_

- [ ] 5. S2 — `deploy.py`: remove the embedded AWS credentials (LAST / highest risk)
  - [ ] 5.1 Remove the credential-export entries and the `-a`/`-s` fragment
    - **Property 1: Fix Checking** - The SSM command strings contain no `access_key`/`secret_key` value
    - Delete the two credential-export list elements from `download_edgemlsdk_release_artifacts` (`f"export AWS_ACCESS_KEY_ID={credentials.access_key}"` and `f"export AWS_SECRET_ACCESS_KEY={credentials.secret_key}"`, ~lines 230–231); keep the adjacent `f"export AWS_DEFAULT_REGION={q_region}"` line
    - Delete exactly ` -a {credentials.access_key} -s {credentials.secret_key}` from the mqtt `run_mqtt_longevity` command string (~line 250); preserve every other byte, including the sibling spec's `shlex.quote`'d `-l {q_longevity_hours} -r {q_region} -m {q_mqtt_endpoint} -n {q_payload_size}` fragments
    - Keep the `credentials = session.get_credentials()` local — it is still passed to `create_instance(credentials, ...)` and `run_commands_via_ssm_with_retry(instance_id, 3, ..., credentials)` as boto3 client kwargs (the caller's own control-plane credentials, NOT a command-string sink); do not remove it
    - Rely on the EC2 IAM instance profile (already supplied as `iam_instance_profile_arn` to `create_instance`) for the `aws s3` / `aws ecr` access via IMDSv2
    - **Verify** the instance-profile role (`iam_role_for_edgemlsdk_longevity_tests`) grants the needed S3 (`panorama-sdk-v2-artifacts`, `edgeml-sdk-longevity-tests`) and ECR access before merging; if it does not, widen the role policy — **never** re-embed keys
    - _Bug_Condition: isBugCondition(X) where X = AWS access/secret keys f-string-interpolated into `AWS-RunShellScript` SSM command strings (S2)_
    - _Expected_Behavior: the `export AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` list entries and the `-a`/`-s` fragment are removed; no credential value reaches the command strings, CloudWatch Logs, or SSM history; access comes from the IAM instance profile_
    - _Preservation: the SSM commands perform the same actions using instance-profile credentials; the rest of the command strings — including the sibling spec's `shlex.quote`'d args — are preserved byte-for-byte except for the removed fragments; the boto3 client kwargs still receive `credentials` (Req 3.2)_
    - _Requirements: 2.2_

  - [ ] 5.2 S2b — guard `run_mqtt_longevity.sh` `aws configure set` on non-empty values (companion follow-up)
    - **Property 1: Fix Checking** - The containerized CLI falls back to the IMDS instance role instead of writing empty static credentials
    - In `src/edgemlsdk/src/test/longevity/mqtt/run_mqtt_longevity.sh`, guard the `aws configure set aws_access_key_id` / `aws_secret_access_key` calls so they run ONLY when a non-empty value is provided, e.g. `[ -n "${aws_access_key_id}" ] && aws configure set aws_access_key_id "${aws_access_key_id}"` (same for the secret key)
    - Keep `aws configure set region ${aws_region}` unchanged (region is not a secret)
    - This prevents `aws configure set aws_access_key_id ""` from writing an empty static credential that would take precedence over and break the IMDS instance-role chain; with the flags dropped in 5.1 the container sources credentials from the instance role via `169.254.169.254`
    - **Note**: verify the instance-profile role grants the S3/ECR access the mqtt path needs; never re-embed keys
    - _Bug_Condition: isBugCondition(X) where X = the mqtt script writing an empty static credential after the `-a`/`-s` fragment is removed (S2b)_
    - _Expected_Behavior: `aws configure set` runs only for non-empty keys; the CLI falls back to the instance-role credential provider; `aws configure set region` is preserved_
    - _Preservation: with legitimate deploy args the mqtt longevity path still runs, sourcing credentials from the instance role (Req 3.2)_
    - _Requirements: 2.2_

- [ ] 6. S10 — repo-audit gate finalize (Req 2.10)
  - [ ] 6.1 Make `secrets_audit.disallowed_hits()` return zero on the fixed tree
    - Keep the audit from task 1 as a runnable check that greps the in-scope tree for the three bug-condition sinks (`log_event_dump`, `cred_in_command`, `unverified_jwt`)
    - Assert zero disallowed hits across `IN_SCOPE_FILES`, allowing ONLY occurrences carrying a documented `# nosem`/`# nosec` exception (the S5 pre-parse line, and the S6–S9 documented markers)
    - Enforce precise in-scope scoping: exclude `cdk.out/asset.*`, the vendored `src/backend/edgemlsdk/edgemlsdk/` duplicate of `deploy.py`, and the security test files' own pattern strings; the gate must still FAIL if a full-event log, an interpolated credential, or an un-annotated `verify_signature=False` is reintroduced into any real in-scope source file
    - _Bug_Condition: isBugCondition(X) where X = any remaining disallowed full-event log / credential interpolation / un-annotated `verify_signature=False` occurrence in in-scope code (S10)_
    - _Expected_Behavior: `disallowed_hits()` returns zero, minus documented justified exceptions; a reintroduced sink still fails the gate_
    - _Preservation: the generated CDK artifacts, the vendored duplicate, and out-of-scope findings are not touched (Req 3.10)_
    - _Requirements: 2.10_

- [ ] 7. Verify the bug-condition exploration test now passes (Fix Checking)
  - **Property 1: Expected Behavior** - Every secret sink closed / pattern documented
  - **IMPORTANT**: Re-run the SAME audit + targeted tests from task 1 - do NOT write new tests
  - Re-run the targeted tests on the fixed tree: S1 — the handler log line contains `methodArn`/`requestId` and NEVER the token or headers; S2 — the constructed command strings contain NO `access_key`/`secret_key`/`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`-a`/`-s` fragment; S5 — a tampered token is still rejected AND the pre-parse line carries the documented `# nosem`
  - Re-run `secrets_audit.disallowed_hits()` over the full in-scope tree
  - **EXPECTED OUTCOME**: No secret is observed at any sink AND the audit returns ZERO disallowed hits (minus the documented `# nosem`/`# nosec` exceptions for S5–S9)
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10_

- [ ] 8. Verify preservation baseline tests still pass (Preservation Checking)
  - **Property 2: Preservation** - No behavior change for legitimate inputs
  - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
  - Run the preservation baselines/property tests under the fix: S1 — identical allow/deny IAM policy for the same (mocked-decode) event; S2 — command strings equal the baseline **minus exactly** the removed credential fragments (byte-for-byte, including the sibling spec's `shlex.quote`'d args); S5 — valid token validates, tampered token rejected; S6 — same bind config; S7 — same pagination; S8 — same bucket/secret value and operations; S9 — the `test_auth.py` suite passes unchanged
  - Confirm the `cdk.out/asset.*` copies, the vendored `edgemlsdk/edgemlsdk/` duplicate, and the boto3 client kwarg lines in `deploy.py` are unchanged
  - **EXPECTED OUTCOME**: Tests PASS (no regressions); `F(X) = F'(X)` for all non-bug-condition inputs
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10_

- [ ] 9. Integration + CI-gate verification
  - [ ] 9.1 Run the backend security suites and wire the audit into `build-custom.sh`
    - Run the backend security suites to completion — `secrets_audit.py`, `test_secrets_bug_condition_exploration.py`, and the `security/preservation` suite — and confirm the secrets/credentials/JWT unit + property tests pass with no regressions
    - Wire the new gate into `build-custom.sh`'s existing security-audit gate block (~lines 207–225, under `set -e`), next to the Group-1 gate:
      - `python${PYTHON_VERSION} test/backend-test/security/secrets_audit.py`
      - `python${PYTHON_VERSION} -m pytest test/backend-test/security/test_secrets_bug_condition_exploration.py -v`
      - `python${PYTHON_VERSION} -m pytest test/backend-test/security/preservation -p no:cacheprovider --noconftest -v`
    - Confirm the gate FAILS the build on a reintroduced sink (full-event log, interpolated credential, or un-annotated `verify_signature=False`) in in-scope application code
    - End-to-end spot checks: invoke the authorizer with a valid token → same allow policy, log line free of the token; with an invalid token → deny policy, no token logged; `deploy.py` with valid args builds command strings free of any key
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10_

- [ ] 10. Checkpoint - Ensure all tests pass and the CI gate is wired
  - Confirm the task-1 audit + targeted tests now observe no secret at any sink and `disallowed_hits()` returns zero (task 7), the task-2 preservation tests still pass (task 8), and the backend security suites + integration checks pass (task 9)
  - Confirm the `secrets_audit.py` gate (plus the exploration and preservation suites) is wired into `build-custom.sh` so a disallowed full-event log / credential interpolation / un-annotated `verify_signature=False` reappearing in in-scope application code fails the build
  - Ensure all tests pass; ask the user if questions arise

---

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "description": "Run on UNFIXED code: surface secret-sink / undocumented-pattern counterexamples (audit + targeted tests) and capture preservation baselines (independent).", "tasks": ["1", "2"] },
    { "wave": 2, "description": "Doc / # nosem-only fixes first (zero runtime risk, comment/literal-text edits, no control-flow change).", "tasks": ["3.1", "3.2", "3.3", "3.4", "3.5", "3.6", "3.7"] },
    { "wave": 3, "description": "S1 log redaction — substantive but low blast radius: only the invocation log line changes; the emitted IAM policy is untouched.", "tasks": ["4.1"] },
    { "wave": 4, "description": "S2 credential removal LAST — highest risk: changes what deploy.py sends to the instance and shifts credential sourcing to the IAM instance profile, with the run_mqtt_longevity.sh cross-file dependency (S2b).", "tasks": ["5.1", "5.2"] },
    { "wave": 5, "description": "Repo-audit gate (S10) — the pattern gate that proves no disallowed sink remains.", "tasks": ["6.1"] },
    { "wave": 6, "description": "Fix Checking and Preservation Checking (re-run tasks 1 and 2 on fixed code).", "tasks": ["7", "8"] },
    { "wave": 7, "description": "Integration + CI-gate verification (backend security suites + wire secrets_audit into build-custom.sh).", "tasks": ["9.1"] },
    { "wave": 8, "description": "Checkpoint: all green + CI gate wired.", "tasks": ["10"] }
  ]
}
```

Visual summary of the critical path:

```
1. Bug-condition audit + targeted tests (FAILS: token in log, keys in commands, no marker on line 131)
2. Preservation baseline tests (PASS on unfixed tree)
        │  (1 and 2 are independent; both run on UNFIXED code first)
        ▼
2. DOC / # nosem-ONLY FIXES (zero runtime risk, land first)
   3.1 S3 packaging email → example.com
   3.2 S4 README placeholder password
   3.3 S5 jwt unverified pre-parse nosem + comment (+ tampered-token assertion)
   3.4 S6 app.py 0.0.0.0 bind nosem ×2
   3.5 S7 components.py pagination_token nosec
   3.6 S8 deploy.py bucket/secret-name nosec ×2
   3.7 S9 test_auth.py token fixtures nosec ×6
        │
        ▼
3. 4.1 S1 log redaction (_safe_event_metadata + redacted log call)
        │                                    ── substantive, low blast radius (policy untouched)
        ▼
4. S2 CREDENTIAL REMOVAL (LAST — highest risk)
   5.1 deploy.py remove export entries + -a/-s fragment (keep boto3 kwargs)
   5.2 S2b run_mqtt_longevity.sh guard aws configure set on non-empty keys
        │                                    ── shifts credential sourcing to the IAM instance profile
        ▼
5. 6.1 S10 repo-audit gate (disallowed_hits() == 0, minus documented exceptions)
        │
        ├──────────────┐
        ▼              ▼
6. 7. Fix Checking    8. Preservation Checking
    (re-run task 1:    (re-run task 2:
     no secret at sink, F(X) = F'(X),
     zero audit hits)   still passes)
        │              │
        └──────┬───────┘
               ▼
7. 9.1 Integration + CI-gate verification (security suites + wire build-custom.sh gate)
               │
               ▼
8. 10. Checkpoint (all green + CI audit guard wired)
```

**Critical path:** 3.1–3.7 → 4.1 → 5.1 → 5.2 → 6.1 → 7/8 → 9.1 → 10.

**Ordering rationale (from the design's "Ordering and risk"):** the doc / `# nosem`-only
changes (S3–S9) land first because they are comment / literal-text edits with no
control-flow impact (preservation is "runtime behavior byte-for-byte identical");
S1 (log redaction) follows — substantive but low blast radius, since only the
invocation log line changes and the emitted IAM policy is untouched; S2 (credential
removal) lands last as the highest-risk change because it alters what `deploy.py`
sends to the instance and shifts credential sourcing to the IAM instance profile,
with a cross-file dependency on `run_mqtt_longevity.sh` (S2b) — so it gets the
strongest preservation checks (command strings equal the baseline minus exactly the
removed fragments) and requires verifying the instance-profile role grants the
needed S3/ECR access before merging. The repo audit (S10) is the final gate that
proves no disallowed sink remains.

## Notes

**Bug-condition methodology reminders:**
- Task 1 is the exploration test — it is EXPECTED to surface non-empty audit hits
  and observe the secret at each sink (token in the S1 log, keys in the S2
  commands, no marker on the S5 line) on the unfixed tree (the counterexamples that
  confirm the bug). Do not "fix" it, and do not modify application source code in
  task 1.
- Task 2 captures preservation baselines that must PASS on the unfixed tree.
- Tasks 7 and 8 re-run the SAME task-1 audit/targeted tests and task-2 baselines
  against the fixed tree — every sink must be closed / documented (zero disallowed
  audit hits) and the preservation tests must still pass.
- The only occurrences allowed to survive the audit are those carrying a documented,
  justified exception: the S5 pre-parse `# nosem` line and the S6–S9 documented
  `# nosem`/`# nosec` markers.
- Property-based testing (Hypothesis, already vendored under `.hypothesis/`) is
  emphasized where the input domain is generatable: S1 (generated events → policy
  invariant + log free of secrets) and S2 (generated valid args → command strings
  equal baseline minus removed fragments).
- The boto3 client kwargs in `deploy.py` (`aws_access_key_id=credentials.access_key`,
  etc.) are NOT a sink — they pass keys to the SDK, not into a command string — and
  must remain unchanged.
