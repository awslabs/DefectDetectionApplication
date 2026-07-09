# Bugfix Requirements Document

## Introduction

A code security review of the DefectDetectionApplication (DDA) — the AWS "DDA
Code Review" scan captured in `security-findings-report.json` at the repo root
(63 findings total) — surfaced a set of application-code findings in a group of
related classes concerning **secrets, credentials, and JWT/token handling**:

1. **Sensitive data handling** — secret-bearing values (bearer tokens, AWS
   access/secret keys) are written to a log sink or interpolated into a shell
   command string that lands in a durable, broadly-readable location
   (CloudWatch Logs, SSM command history), where they are exposed.
2. **Secrets and credentials** — a real-looking credential literal is present in
   a copy-pasteable location (documentation) where it can be mistaken for, or
   copied as, a real secret.
3. **Scanner-flagged patterns without a documented exception** — an
   intentionally unverified JWT pre-parse, an intentional `0.0.0.0` bind for an
   edge-appliance LAN UI, and several Bandit B105/B106 "hardcoded password"
   false positives (empty strings, a non-secret bucket name, test-only token
   literals) are flagged by the scanner but are correct as written; they lack an
   in-code, documented exception recording why they are safe.

This spec ("Secrets, credentials & JWT/token hardening" — the next remediation
group after `security-injection-deserialization-fixes`, which fixed findings
#1–#8, injection/deserialization) is scoped **strictly** to the findings
enumerated below (labelled S1–S9). It uses the SAME bug-condition methodology,
EARS format, and Property 1 (Fix Checking) / Property 2 (Preservation) framing as
that sibling spec.

**Explicitly out of scope (handled in different batches):**

- The vendored `requests/auth.py` B324 `md5`/`sha1` findings and the vendored
  `urllib3/connection.py` findings are **dependency / supply-chain** items in
  third-party vendored code; they are remediated in a separate supply-chain
  batch and are NOT touched here.
- Injection / unsafe-deserialization (findings #1–#8) — already remediated in the
  sibling spec `security-injection-deserialization-fixes`.
- Any other finding class from the 63-finding report not listed as S1–S9 below.

**Important:** duplicate / generated copies of any of the in-scope files (e.g.
under `edge-cv-portal/infrastructure/cdk.out/asset.*` or other vendored/generated
trees) are build artifacts that regenerate from source and are **not** in scope;
only the real source paths listed below are to be fixed. All findings were triaged
against the real source, not the vendored/generated copies.

The findings and their real source locations:

**True-positive code fixes**

| # | File | Loc | Finding | Fix |
|---|------|-----|---------|-----|
| S1 | `edge-cv-portal/backend/functions/jwt_authorizer.py` | 272 | `handler()` logs the full API Gateway event via `logger.info(f"JWT Authorizer invoked with event: {json.dumps(event, default=str)}")`; the event carries `authorizationToken` and `headers` (bearer tokens). | Log only non-sensitive metadata (e.g. `methodArn`, `requestContext.requestId`); never log the token or headers. |
| S2 | `src/edgemlsdk/src/test/longevity/deploy.py` | ~230–231, ~250 | AWS credentials interpolated into `AWS-RunShellScript` SSM command strings: `f"export AWS_ACCESS_KEY_ID={credentials.access_key}"` / `f"export AWS_SECRET_ACCESS_KEY={credentials.secret_key}"` in `download_edgemlsdk_release_artifacts`, and `-a {credentials.access_key} -s {credentials.secret_key}` in the mqtt `run_mqtt_longevity` command. These land in CloudWatch Logs / SSM command history. | Remove the credential-export / `-a`/`-s` fragments; rely on the EC2 IAM instance profile (already passed as `iam_instance_profile_arn` to `create_instance`) for AWS access. |
| S3 | `edge-cv-portal/backend/functions/packaging.py` | 448 (`system@edgecv.com` at ~440) | Synthetic email `system@edgecv.com` uses a real registrable domain. | Change to `system@example.com` (RFC 2606). |
| S4 | `README.md` | ~208 | A copy-pasteable Cognito admin command contains a real-looking password `YourSecurePassword1234!`. | Replace with an obvious placeholder, e.g. `<YOUR_SECURE_PASSWORD>`. Doc-only. |

**By-design — documented `# nosem` + rationale (no behavior change)**

| # | File | Loc | Finding | Fix |
|---|------|-----|---------|-----|
| S5 | `edge-cv-portal/backend/functions/jwt_authorizer.py` | 131 | `jwt.decode(token, options={"verify_signature": False})` (unverified-jwt-decode) — an INTENTIONAL pre-parse to read `kid`/`iss` before the JWKS key is known; a FULL signature-verified decode follows at ~line 178 (`jwt.decode(token, public_key, algorithms=['RS256'], … verify_exp/aud/iss)`). | Add a documented `# nosem` + comment explaining the unverified read is only for routing and that full verification is enforced afterward. |
| S6 | `src/backend/app.py` | 271, 274 | B104 bind `0.0.0.0` — the on-device operator-UI server binds `0.0.0.0`: port 5443 WITH TLS + station authorization, and plaintext 5000 ONLY when `utils.is_authorization_enabled_on_station()` is False. Intentional for an edge appliance serving the LAN UI. | Documented `# nosem` + rationale on both lines (noting the plaintext path is auth-disabled-only). No behavior change. |

**Bandit false positives — documented `# nosem` (no behavior change)**

| # | File | Loc | Finding | Fix |
|---|------|-----|---------|-----|
| S7 | `edge-cv-portal/backend/functions/components.py` | 305 (`pagination_token = ''`) | B105 "hardcoded password: ''" — an empty-string literal, not a secret. | `# nosem` + one-line rationale. |
| S8 | `src/edgemlsdk/src/test/longevity/deploy.py` | ~59, ~207 | B105 — a non-secret S3 bucket / secret name (`'edgeml-sdk-longevity-tests'`) flagged as a password. | `# nosem` rationale. |
| S9 | `test/backend-test/utils/test_auth.py` | 47, 60, 71, 83, 95, 106 | B106 — test-only token literals (`'good-token'`, `'some-token'`, `'inactive-token'`, `''`) passed as function args; not real credentials. | `# nosem` rationale on each. |

The recommended remediations (per the report and the located source) are:
redact the logged event to non-sensitive metadata (S1); remove the embedded
credential fragments and rely on the IAM instance profile (S2); use an RFC 2606
reserved domain for the synthetic email (S3); use an obvious placeholder in the
doc command (S4); add a documented `# nosem` + rationale for the intentional
unverified pre-parse (S5) and the intentional LAN-UI bind (S6); and add a
documented `# nosem` + rationale for the Bandit false positives (S7, S8, S9).

This requirement also drives a **repo audit** (mirroring the sibling spec's
finding #9): a runnable check that greps in-scope code for secret-bearing log /
command sinks and un-annotated scanner patterns (`json.dumps(event` in a logging
call; `access_key`/`secret_key` interpolated into command lists;
`verify_signature.*False` without a documented `# nosem`) and asserts zero
disallowed hits, minus documented exceptions. It must surface these
counterexamples on the unfixed tree and return zero disallowed hits after the fix.

### Bug Condition and Properties

The bug-condition methodology frames this fix as follows.

**Bug Condition `C(X)`** — identifies the inputs/code paths that trigger the
defect. Here the "input" is any application-code path that either carries a
secret value into a log / command sink, exposes a real-looking credential in a
copy-pasteable location, or presents a scanner-flagged pattern with no documented
exception:

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type CodePath   // an application code path that touches a secret,
                              // a log/command sink, or a scanner-flagged pattern
  OUTPUT: boolean

  // True when a secret value reaches a durable/broadly-readable sink, OR a
  // real-looking credential sits in a copy-pasteable location, OR a
  // scanner-flagged pattern lacks a documented, justified exception.
  RETURN secretReachesSink(X)              // token/credential -> log or command
      OR realCredentialInCopyableText(X)   // real-looking secret in docs/source
      OR flaggedPatternWithoutException(X) // nosem-worthy pattern, undocumented
END FUNCTION
```

**Fix Property `P` (Fix Checking)** — desired behavior for all buggy inputs after
the fix `F'`:

```pascal
// Property: Fix Checking - the secret sink is closed / the pattern is neutralized
//                          or documented
FOR ALL X WHERE isBugCondition(X) DO
  result ← F'(X)
  ASSERT neutralized(result)
     // sensitive data: the log / command sink NO LONGER receives the secret
     //   (redacted to non-sensitive metadata, or the credential fragment is
     //   removed and access comes from the IAM instance profile).
     // secrets in text: the real-looking credential is replaced by an obvious
     //   placeholder / reserved-domain value that cannot be mistaken for a
     //   real secret.
     // flagged pattern: the pattern is EITHER removed OR carries a documented
     //   `# nosem` + rationale recording why it is safe (and, for the
     //   unverified JWT pre-parse, that full verification is enforced after).
END FOR
```

**Preservation Property (Preservation Checking)** — for every input that does NOT
trigger the bug condition, the fixed code behaves identically to the original
code `F`:

```pascal
// Property: Preservation Checking - no behavior change for legitimate paths
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
     // the JWT authorizer still generates the same allow/deny IAM policy for the
     // same token; the SSM commands still perform the same actions using the
     // instance-profile credentials; packaging still produces the same result;
     // the LAN-UI server still binds the same host/port; every documented-only
     // change (nosem, doc placeholder) leaves runtime behavior byte-for-byte
     // identical.
END FOR
```

- **F**: the original (unfixed) code, where the secret reaches the sink / the
  real credential sits in copyable text / the pattern is un-annotated.
- **F'**: the fixed code, where the sink is redacted, the credential fragment is
  removed, the literal is a placeholder, or the pattern carries a documented
  exception.

Where the input domain is generatable (e.g. API Gateway events carrying arbitrary
token/header values, deploy argument values, JWT tokens), **property-based
testing** is emphasized: generate events with secret-bearing fields and assert
the emitted log line never contains the token/headers (Fix Checking) while the
returned IAM policy is unchanged (Preservation); generate valid and tampered
tokens and assert the tampered token is still rejected by the verified decode.

## Bug Analysis

### Current Behavior (Defect)

The application exposes secret values to log / command sinks, embeds a
real-looking credential in copy-pasteable text, and leaves several intentional
scanner-flagged patterns undocumented.

1.1 WHEN `handler(event, lambda_context)` in
`edge-cv-portal/backend/functions/jwt_authorizer.py` (line 272) is invoked THEN
the system logs the full API Gateway authorizer event via
`logger.info(f"JWT Authorizer invoked with event: {json.dumps(event, default=str)}")`,
and because the event carries `authorizationToken` and `headers` (bearer tokens),
the complete token is written to CloudWatch Logs, exposing the credential to
anyone with log-read access.

1.2 WHEN `src/edgemlsdk/src/test/longevity/deploy.py` builds its
`AWS-RunShellScript` SSM command lists THEN the system interpolates live AWS
credentials into the command strings —
`f"export AWS_ACCESS_KEY_ID={credentials.access_key}"` and
`f"export AWS_SECRET_ACCESS_KEY={credentials.secret_key}"` in
`download_edgemlsdk_release_artifacts` (~lines 230–231), and
`-a {credentials.access_key} -s {credentials.secret_key}` in the mqtt
`run_mqtt_longevity` command (~line 250) — so the access key and secret key are
persisted in CloudWatch Logs and SSM command-invocation history.

1.3 WHEN the packaging Lambda in
`edge-cv-portal/backend/functions/packaging.py` (line ~448, literal at ~440)
constructs a synthetic system identity THEN the system uses the email
`system@edgecv.com`, which is a real, registrable domain rather than an RFC 2606
reserved example domain.

1.4 WHEN a reader follows the Cognito admin setup instructions in `README.md`
(line ~208) THEN the system's documentation presents a copy-pasteable
`aws cognito-idp admin-set-user-password … --password YourSecurePassword1234!`
command containing a real-looking password literal that can be copied verbatim as
a credential.

1.5 WHEN `validate_jwt_token(token)` in
`edge-cv-portal/backend/functions/jwt_authorizer.py` (line 131) pre-parses the
token via `jwt.decode(token, options={"verify_signature": False})` to read `kid`
and `iss` THEN a security scanner flags an unverified-jwt-decode, even though this
is an intentional pre-parse to select the JWKS key and a full signature-verified
decode follows at ~line 178; the intent and the enforced downstream verification
are not documented in-code, so the finding has no recorded exception.

1.6 WHEN `main()` in `src/backend/app.py` (lines 271 and 274) starts the on-device
operator-UI server THEN the system binds `host="0.0.0.0"` — port 5443 with TLS
and station authorization enabled, and plaintext port 5000 only when
`utils.is_authorization_enabled_on_station()` is False — which a scanner flags as
B104; this is intentional for an edge appliance serving the LAN UI but has no
in-code documented exception.

1.7 WHEN Bandit scans `edge-cv-portal/backend/functions/components.py` (line 305,
`pagination_token = ''`) THEN it reports B105 "hardcoded password" for the
empty-string literal, which is not a secret and has no recorded exception.

1.8 WHEN Bandit scans `src/edgemlsdk/src/test/longevity/deploy.py` (the
non-secret S3 bucket / secret name literal `'edgeml-sdk-longevity-tests'` at
~lines 59 and 207) THEN it reports B105 "hardcoded password" for a value that is a
bucket/secret name, not a password, and has no recorded exception.

1.9 WHEN Bandit scans `test/backend-test/utils/test_auth.py` (lines 47, 60, 71,
83, 95, 106) THEN it reports B106 "hardcoded password" for the test-only token
literals (`''`, `'good-token'`, `'inactive-token'`, `'some-token'`) passed as
`token=` arguments, which are test fixtures, not real credentials, and have no
recorded exception.

1.10 WHEN the repository is audited for the bug-condition patterns (secret-bearing
log/command sinks: `json.dumps(event` inside a logging call, `access_key` /
`secret_key` interpolated into command lists; and `verify_signature.*False`
without a documented `# nosem`) THEN the unfixed tree contains the disallowed
occurrences above with no documented, justified exception.

### Expected Behavior (Correct)

The application redacts secrets before they reach a sink, removes embedded
credentials in favor of the IAM instance profile, replaces real-looking
credentials with obvious placeholders, and records a documented exception for
each intentional scanner-flagged pattern.

2.1 WHEN `handler()` in `jwt_authorizer.py` (line 272) is invoked THEN the system
SHALL log only non-sensitive metadata (e.g. `methodArn`, and
`requestContext.requestId` when present) and SHALL NOT log `authorizationToken`,
`headers`, or any other token-bearing field, so no bearer token reaches
CloudWatch Logs.

2.2 WHEN `deploy.py` builds its `AWS-RunShellScript` SSM command lists THEN the
system SHALL remove the `export AWS_ACCESS_KEY_ID=…` / `export
AWS_SECRET_ACCESS_KEY=…` fragments and the mqtt `-a {access_key} -s {secret_key}`
fragments, and SHALL rely on the EC2 IAM instance profile (already supplied as
`iam_instance_profile_arn` to `create_instance`) for AWS access, so no credential
value is written into the command strings, CloudWatch Logs, or SSM command
history.

2.3 WHEN `packaging.py` constructs the synthetic system identity THEN the system
SHALL use an RFC 2606 reserved domain, i.e. `system@example.com`, instead of a
real registrable domain.

2.4 WHEN the `README.md` Cognito admin setup command is presented THEN the system
documentation SHALL use an obvious, non-copyable placeholder such as
`<YOUR_SECURE_PASSWORD>` instead of a real-looking password literal.

2.5 WHEN `validate_jwt_token` performs the unverified pre-parse
`jwt.decode(token, options={"verify_signature": False})` (line 131) THEN the
system SHALL carry a documented `# nosem` annotation and a comment explaining that
the unverified read is used ONLY to obtain `kid`/`iss` for JWKS-key selection and
that a full signature-verified decode (`RS256`, `verify_exp`/`verify_aud`/
`verify_iss`) is enforced afterward at ~line 178, so the flagged pattern carries a
justified exception; and a tampered token SHALL still be rejected by the verified
decode.

2.6 WHEN `app.py` binds the LAN-UI server on `0.0.0.0` (lines 271 and 274) THEN
the system SHALL carry a documented `# nosem` annotation and rationale on both
lines recording that the bind is intentional for an edge appliance serving the LAN
UI, that port 5443 is TLS-protected with station authorization, and that the
plaintext 5000 path is reachable only when station authorization is disabled.

2.7 WHEN Bandit scans the empty-string literal `pagination_token = ''` in
`components.py` (line 305) THEN the system SHALL carry a documented `# nosem`
annotation with a one-line rationale recording that the value is an empty pagination
cursor, not a secret.

2.8 WHEN Bandit scans the non-secret bucket / secret name literal
`'edgeml-sdk-longevity-tests'` in `deploy.py` (~lines 59 and 207) THEN the system
SHALL carry a documented `# nosem` annotation with a rationale recording that the
value is an S3 bucket / secret name, not a password.

2.9 WHEN Bandit scans the test-only token literals in
`test/backend-test/utils/test_auth.py` (lines 47, 60, 71, 83, 95, 106) THEN the
system SHALL carry a documented `# nosem` annotation with a rationale on each,
recording that the values are test fixtures, not real credentials.

2.10 WHEN the repository is audited for the bug-condition patterns (secret-bearing
log/command sinks — `json.dumps(event` in a logging call, `access_key` /
`secret_key` interpolated into command lists — and `verify_signature.*False`
without a documented `# nosem`) THEN the system SHALL contain no remaining
disallowed occurrence in the in-scope application code, other than occurrences
carrying a documented, justified exception (a `# nosem` / comment recording why
the pattern is safe). The audit SHALL be runnable and SHALL assert zero disallowed
hits minus documented exceptions.

### Unchanged Behavior (Regression Prevention)

All legitimate behavior must continue to work exactly as before. For every input
that does NOT trigger the bug condition, the fixed system must behave identically
to the original. Every change in S4–S9 is documentation-only or comment-only and
must leave runtime behavior byte-for-byte identical.

3.1 WHEN the JWT authorizer processes a token THEN the system SHALL CONTINUE TO
extract the token, validate it, and generate the same allow/deny IAM policy (same
`principalId`, effect, resource, and context) for the same input as before; only
the content of the invocation log line changes.

3.2 WHEN `deploy.py` runs with legitimate arguments THEN the system SHALL CONTINUE
TO deploy and run the longevity tests, executing the same SSM command semantics
using the EC2 IAM instance-profile credentials, and SHALL CONTINUE TO preserve the
rest of the SSM command strings — including the `shlex.quote`'d argument fragments
introduced by the sibling injection spec — byte-for-byte, except for the removed
credential-export / `-a`/`-s` fragments.

3.3 WHEN the packaging Lambda constructs the synthetic system identity THEN the
system SHALL CONTINUE TO behave identically apart from the email domain value, and
all other packaging behavior SHALL remain unchanged.

3.4 WHEN a reader uses the `README.md` Cognito setup instructions THEN the
documentation SHALL CONTINUE TO describe the same command with the same flags and
structure; only the password token becomes an explicit placeholder.

3.5 WHEN `validate_jwt_token` runs its two-stage decode THEN the system SHALL
CONTINUE TO perform the unverified pre-parse for `kid`/`iss` selection and the
full signature-verified decode exactly as before; the `# nosem` annotation and
comment SHALL NOT change any control flow, and a valid token SHALL still validate
while a tampered token is still rejected.

3.6 WHEN the on-device server starts THEN the system SHALL CONTINUE TO bind the
same host (`0.0.0.0`) and the same ports (5443 with TLS when authorization is
enabled, 5000 plaintext when it is disabled) with the same uvicorn configuration;
the `# nosem` annotations SHALL NOT alter the bind behavior.

3.7 WHEN `components.py` lists private components THEN the system SHALL CONTINUE TO
initialize and use the empty `pagination_token` cursor exactly as before; the
`# nosem` annotation SHALL NOT change pagination behavior.

3.8 WHEN `deploy.py` references the `'edgeml-sdk-longevity-tests'` bucket / secret
name THEN the system SHALL CONTINUE TO use the same value for the same S3 / Secrets
Manager operations; the `# nosem` annotation SHALL NOT change any value or call.

3.9 WHEN the `test_auth.py` suite runs THEN the system SHALL CONTINUE TO exercise
the same `auth.validate_token` scenarios with the same token fixtures and the same
assertions, and all tests SHALL continue to pass; the `# nosem` annotations SHALL
NOT change any test input or outcome.

3.10 WHEN the review's out-of-scope findings are considered — the vendored
`requests/auth.py` B324 `md5`/`sha1` findings and the vendored
`urllib3/connection.py` findings (dependency / supply-chain, handled in a separate
batch), the already-remediated injection/deserialization findings #1–#8, and any
generated / vendored duplicate copies of the in-scope files — THEN this spec SHALL
CONTINUE TO leave them unchanged.
