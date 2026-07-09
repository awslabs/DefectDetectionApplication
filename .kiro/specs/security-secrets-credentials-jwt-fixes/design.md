# Secrets, Credentials & JWT/Token Hardening (Group 2) Bugfix Design

## Overview

A code security review of the DefectDetectionApplication (DDA) — the AWS "DDA
Code Review" scan captured in `security-findings-report.json` (63 findings) —
surfaced a group of application-code findings concerning **secrets, credentials,
and JWT/token handling**. This spec is the second remediation group (the sibling
`security-injection-deserialization-fixes` covered injection / deserialization,
findings #1–#8); it is scoped **strictly** to findings **S1–S9** plus the
repo-audit gate (Req 2.10).

The defect, across nine sites, is one of three shapes:

1. **A secret value reaches a durable, broadly-readable sink.** The JWT authorizer
   logs the entire API Gateway event (which carries `authorizationToken` and
   `headers` bearer tokens) to CloudWatch Logs (S1). The longevity `deploy.py`
   interpolates live AWS access/secret keys into `AWS-RunShellScript` SSM command
   strings, which persist in CloudWatch Logs and SSM command-invocation history
   (S2).
2. **A real-looking credential sits in copy-pasteable text.** `packaging.py` uses
   a real registrable domain (`system@edgecv.com`) for a synthetic identity (S3),
   and `README.md` presents a copy-pasteable admin command with a real-looking
   password literal (S4).
3. **A scanner-flagged pattern lacks a documented, justified exception.** An
   intentional unverified JWT pre-parse (S5), an intentional `0.0.0.0` LAN-UI bind
   (S6), and several Bandit B105/B106 false positives — an empty-string pagination
   cursor (S7), a non-secret bucket/secret name (S8), and test-only token literals
   (S9) — are all correct as written but carry no in-code `# nosem` + rationale.

The fix **closes the sink** (redact the logged event to non-sensitive metadata;
remove the embedded credential fragments and rely on the EC2 IAM instance
profile), **neutralizes the copy-pasteable credential** (RFC 2606 reserved domain;
an obvious placeholder), and **documents each intentional pattern** (`# nosem` +
rationale) — while keeping the behavior for every legitimate input **byte-for-byte
identical** (`F(X) = F'(X)` for all non-buggy inputs). Every change in S3–S9 is
documentation-only or comment-only and changes no runtime control flow; only S1
(log content) and S2 (command strings) touch executed code, and both are designed
so the observable non-secret behavior — the emitted IAM policy, the SSM actions —
is unchanged.

The fix splits into two risk tiers:

1. **Doc / `# nosem`-only changes (zero runtime risk, land first):** S3, S4, S5,
   S6, S7, S8, S9. These are comment / literal-text edits with no control-flow
   impact; preservation is proven by "runtime behavior byte-for-byte identical".
2. **Substantive code changes (land last, strongest preservation checks):** S1
   (log redaction) and S2 (credential removal). S2 is the **highest risk** because
   it changes what `deploy.py` sends to the instance and shifts credential
   sourcing to the IAM instance profile.

A **repo audit** (mirroring the sibling spec's finding #9 / `repo_audit.py`) is the
exploration test and CI gate: it greps in-scope application code for the
bug-condition sinks — a logging call whose argument contains `json.dumps(event`,
`access_key`/`secret_key` interpolated into a command/list string, and
`verify_signature` set `False` without a documented `# nosem` — and asserts zero
disallowed hits after the fix (minus documented exceptions). It must be non-empty
on the unfixed tree and zero-disallowed after.

Duplicate / generated copies of any in-scope file (e.g. under
`edge-cv-portal/infrastructure/cdk.out/asset.*`, or the vendored
`src/backend/edgemlsdk/edgemlsdk/` duplicate of `deploy.py`) are build artifacts
that regenerate from source and are **out of scope**; only the real source paths
are fixed, and the audit is scoped to exclude them.

## Glossary

- **Bug_Condition (C)**: A `CodePath` that either carries a secret value into a
  log / command sink, exposes a real-looking credential in copy-pasteable text, or
  presents a scanner-flagged pattern with no documented exception — formally
  `secretReachesSink(X) OR realCredentialInCopyableText(X) OR flaggedPatternWithoutException(X)`.
- **Property (P) / Fix Checking**: After the fix, for every buggy input the sink is
  **closed** (the log / command no longer receives the secret — redacted to
  non-sensitive metadata, or the credential fragment is removed and access comes
  from the IAM instance profile), the real-looking credential is replaced by an
  **obvious placeholder / reserved-domain value**, or the flagged pattern is
  **removed or carries a documented `# nosem` + rationale**.
- **Preservation**: For every input that does NOT trigger the bug condition, the
  fixed code behaves identically to the original — `F(X) = F'(X)`. The authorizer
  emits the **same allow/deny IAM policy** for the same token; the SSM commands
  perform the **same actions** using instance-profile credentials; packaging,
  pagination, the LAN-UI bind, and the auth tests are unchanged; every
  documentation-only change leaves runtime behavior byte-for-byte identical.
- **F / F'**: The original (unfixed) code where the secret reaches the sink / the
  real credential sits in copyable text / the pattern is un-annotated; and the
  fixed code where the sink is redacted, the fragment removed, the literal is a
  placeholder, or the pattern carries a documented exception.
- **Secret-bearing sink**: A durable, broadly-readable destination — CloudWatch
  Logs, SSM command-invocation history — that a bearer token or AWS key must never
  reach.
- **IAM instance profile**: The role attached to the EC2 instance via
  `iam_instance_profile_arn` (already passed to `create_instance`), which grants
  the instance S3 / ECR / IoT access through the instance-metadata credential
  provider chain (IMDSv2), removing the need to embed static keys.
- **`# nosem`**: The Semgrep in-line suppression marker; paired here with a
  human-readable rationale recording why the flagged pattern is safe. Bandit
  `# nosec` is the analogous marker for the B104/B105/B106 findings.
- **`_safe_event_metadata(event)`**: A new helper in `jwt_authorizer.py` that
  extracts ONLY non-sensitive fields (`methodArn`, and `requestContext.requestId`
  when present) from the authorizer event, so the invocation log line never
  contains `authorizationToken` or `headers`.

## Bug Details

### Bug Condition

The bug manifests on any application-code path that (a) carries a secret value
(bearer token, AWS access/secret key) into a log or command sink, (b) exposes a
real-looking credential in a copy-pasteable location, or (c) presents a
scanner-flagged pattern with no documented, justified exception. The nine sites
are: full-event log in `handler` (S1); AWS-key interpolation into SSM commands in
`deploy.py` (S2); real-domain synthetic email in `packaging.py` (S3); real-looking
password in `README.md` (S4); the unverified JWT pre-parse in `validate_jwt_token`
(S5); the `0.0.0.0` LAN-UI bind in `app.py` (S6); the empty pagination cursor in
`components.py` (S7); the bucket/secret name in `deploy.py` (S8); the test-only
token literals in `test_auth.py` (S9).

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X of type CodePath   // an application code path that touches a secret,
                              // a log/command sink, or a scanner-flagged pattern
  OUTPUT: boolean

  RETURN secretReachesSink(X)              // token/credential -> log or command
      OR realCredentialInCopyableText(X)   // real-looking secret in docs/source
      OR flaggedPatternWithoutException(X) // nosem-worthy pattern, undocumented
END FUNCTION
```

**Expected behavior for buggy inputs (Fix Checking):**
```
FOR ALL X WHERE isBugCondition(X) DO
  result := F'(X)
  ASSERT neutralized(result)
     // sensitive data: the log / command sink NO LONGER receives the secret
     //   (redacted to non-sensitive metadata, or the credential fragment is
     //   removed and access comes from the IAM instance profile).
     // secrets in text: the real-looking credential is replaced by an obvious
     //   placeholder / reserved-domain value that cannot be mistaken for a
     //   real secret.
     // flagged pattern: the pattern is EITHER removed OR carries a documented
     //   `# nosem` + rationale recording why it is safe (and, for the unverified
     //   JWT pre-parse, that full verification is enforced afterward).
END FOR
```

### Examples

Secret reaches a sink (bug manifestation on unfixed code):

- S1: `handler(event, ctx)` where
  `event = {"authorizationToken": "Bearer eyJ...<jwt>", "headers": {"Authorization": "Bearer eyJ..."}, "methodArn": "arn:aws:execute-api:..."}`
  logs `JWT Authorizer invoked with event: {"authorizationToken": "Bearer eyJ...", ...}` — the full bearer token lands in CloudWatch Logs.
  Expected after fix: the log line contains only `{"methodArn": "arn:aws:execute-api:...", "requestId": "..."}` and no token.
- S2: `deploy.py` builds the SSM command list containing
  `export AWS_ACCESS_KEY_ID=AKIA...` / `export AWS_SECRET_ACCESS_KEY=...` and
  `... -a AKIA... -s <secret>` — the live keys persist in SSM command history and CloudWatch Logs.
  Expected after fix: those fragments are gone; the instance uses its IAM instance-profile credentials.

Real credential in copy-pasteable text (bug manifestation on unfixed code):

- S3: `'email': 'system@edgecv.com'` — a real registrable domain.
  Expected after fix: `'system@example.com'` (RFC 2606 reserved).
- S4: README `--password YourSecurePassword1234!` — copy-pasteable real-looking password.
  Expected after fix: `--password <YOUR_SECURE_PASSWORD>` (obvious placeholder).

Flagged pattern without exception (bug manifestation on unfixed code):

- S5: `jwt.decode(token, options={"verify_signature": False})` at line 131 — flagged
  unverified-jwt-decode, no in-code note that it is a routing-only pre-parse and
  that full verification follows at ~line 178. Expected after fix: documented
  `# nosem` + comment; behavior unchanged; a tampered token still rejected.
- S6: `host="0.0.0.0"` on lines 271 and 274 — flagged B104, no rationale. Expected
  after fix: `# nosem`/`# nosec` + rationale on both lines; bind unchanged.
- S7: `pagination_token = ''` — flagged B105 for an empty string. Expected after
  fix: `# nosec` + one-line rationale; value unchanged.
- S8: `secret_name = "edgeml-sdk-longevity-tests"` (and the `bucket_name` literal) —
  flagged B105 for a bucket/secret name. Expected after fix: `# nosec` + rationale.
- S9: `auth.validate_token(token="good-token")` and the other five test-only token
  literals (lines 47, 60, 71, 83, 95, 106) — flagged B106. Expected after fix:
  `# nosec` + rationale on each; tests still pass.
- Edge (preserved, NOT buggy): the authorizer's other log line
  `logger.info(f"Authorization successful for user: {user_id}")` logs a non-secret
  subject id and must remain unchanged; the boto3 client kwargs
  `aws_access_key_id=credentials.access_key` in `create_instance` /
  `run_commands_via_ssm_with_retry` pass the key to the SDK (not into a command
  string) and must remain unchanged.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- The JWT authorizer still extracts the token, validates it, and generates the
  **same allow/deny IAM policy** (same `principalId`, `Effect`, `Resource`, and
  `context`) for the same input; only the content of the invocation log line
  changes (Req 3.1).
- `deploy.py` still deploys and runs the longevity tests, executing the **same SSM
  command semantics** using the EC2 IAM instance-profile credentials, and
  preserves the rest of the SSM command strings — **including the `shlex.quote`'d
  argument fragments introduced by the sibling injection spec** — byte-for-byte,
  except for the removed credential-export / `-a`/`-s` fragments (Req 3.2).
- `packaging.py` behaves identically apart from the email domain value (Req 3.3).
- The README describes the same command with the same flags and structure; only
  the password token becomes an explicit placeholder (Req 3.4).
- `validate_jwt_token` performs the same two-stage decode; a valid token still
  validates and a tampered token is still rejected; the `# nosem` changes no
  control flow (Req 3.5).
- `app.py` binds the same host (`0.0.0.0`) and ports (5443 TLS when authorization
  is enabled, 5000 plaintext when disabled) with the same uvicorn config (Req 3.6).
- `components.py` initializes and uses the empty `pagination_token` cursor exactly
  as before (Req 3.7).
- `deploy.py` uses the same `'edgeml-sdk-longevity-tests'` value for the same S3 /
  Secrets Manager operations (Req 3.8).
- `test_auth.py` exercises the same scenarios with the same fixtures and passes
  unchanged (Req 3.9).

**Scope:**
All inputs that do NOT trigger the bug condition must be completely unaffected.
This explicitly includes:
- Any authorizer event whose non-secret fields drive the policy decision (the
  policy output is a pure function of the token/claims, not of the log line).
- The boto3-client credential kwargs in `deploy.py` (`aws_access_key_id=...`,
  `aws_secret_access_key=...`, `aws_session_token=...`), which pass keys to the
  SDK, not into a command string, and are NOT a sink.
- The review's out-of-scope findings — the vendored `requests/auth.py` B324
  `md5`/`sha1` and `urllib3/connection.py` findings (dependency / supply-chain,
  separate batch), the already-remediated injection/deserialization findings #1–#8,
  and any generated / vendored duplicate copies of the in-scope files (Req 3.10).

**Note:** The expected correct behavior for buggy inputs is defined in the
Correctness Properties section (Property 1); this section focuses on what must NOT
change.

## Hypothesized Root Cause

The code was written for a trusted, single-tenant operational context, so
observability and convenience were favored over secret hygiene, and intentional
scanner-flagged patterns were never annotated. Concretely:

1. **Verbose invocation logging (S1).** `handler` logs the entire event with
   `json.dumps(event, default=str)` for debuggability, without recognizing that
   an API Gateway *authorizer* event's whole purpose is to carry the bearer token
   (in `authorizationToken` and/or `headers`), so the token is dumped verbatim to
   CloudWatch Logs.

2. **Static keys embedded for expedience (S2).** `deploy.py` was written to hand
   AWS credentials to the remote instance the simplest way possible — by exporting
   them in the shell script and passing them as `-a`/`-s` flags — predating (or
   ignoring) the fact that an **IAM instance profile is already attached** to the
   instance (`iam_instance_profile_arn` is passed to `create_instance`). The
   instance-metadata credential provider chain already satisfies the S3/ECR/IoT
   access these commands need, so the embedded keys are redundant as well as
   leaked.

3. **Real-looking placeholders (S3, S4).** A real registrable domain and a
   real-looking password string were used as stand-ins without using the
   RFC 2606 reserved domains / an obvious `<PLACEHOLDER>` convention, so a scanner
   (and a human copying the command) cannot tell they are synthetic.

4. **Undocumented-by-design patterns (S5–S9).** The unverified JWT pre-parse, the
   `0.0.0.0` LAN-UI bind, and the Bandit B105/B106 hits are all correct as written,
   but the intent and the enforced downstream controls (full verified decode after
   the pre-parse; TLS + station-auth on the bind) were never recorded in-code, so
   the scanner has no signal that they are safe.

## Correctness Properties

Property 1: Bug Condition — Secret sinks closed, credentials neutralized, patterns documented

_For any_ code path where the bug condition holds (`isBugCondition` returns true —
a secret value reaches a log / command sink, a real-looking credential sits in
copy-pasteable text, or a scanner-flagged pattern lacks a documented exception),
the fixed code SHALL neutralize it: the JWT authorizer invocation log contains
ONLY non-sensitive metadata (`methodArn`, and `requestContext.requestId` when
present) and never `authorizationToken`, `headers`, or any token-bearing field;
the `deploy.py` SSM commands contain NO `access_key`/`secret_key` value (the
`export AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` and `-a`/`-s` fragments are
removed and access comes from the IAM instance profile); the synthetic email uses
an RFC 2606 reserved domain (`system@example.com`) and the README password is an
obvious placeholder (`<YOUR_SECURE_PASSWORD>`); and each intentional
scanner-flagged pattern (unverified pre-parse, `0.0.0.0` bind, B105/B106 literals)
carries a documented `# nosem`/`# nosec` + rationale — with the unverified JWT
pre-parse recording that a full signature-verified decode is enforced afterward,
so a tampered token is still rejected. A full-repo audit for the bug-condition
patterns finds no remaining disallowed occurrence in in-scope application code,
other than occurrences carrying a documented, justified exception.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10**

Property 2: Preservation — No behavior change for legitimate inputs

_For any_ code path where the bug condition does NOT hold (`isBugCondition`
returns false), the fixed code SHALL produce the same result as the original code
(`F(X) = F'(X)`), preserving: the allow/deny IAM policy the authorizer emits for a
given token; the SSM command strings and actions for valid deploy args (minus only
the removed credential fragments, and including the sibling spec's `shlex.quote`'d
fragments byte-for-byte); the packaging result apart from the email domain; the
README command structure apart from the password placeholder; the two-stage JWT
decode (valid token validates, tampered token rejected); the LAN-UI host/port bind
and uvicorn config; the empty `pagination_token` pagination behavior; the
`'edgeml-sdk-longevity-tests'` value and its S3 / Secrets Manager operations; and
the `test_auth.py` suite inputs, assertions, and pass/fail outcomes.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10**

## Fix Implementation

### Changes Required

Assuming the root-cause analysis is correct, each site gets the minimal change
that makes `isBugCondition` false for it while preserving `F(X) = F'(X)`.

#### S1 — `edge-cv-portal/backend/functions/jwt_authorizer.py` (Req 2.1) — log redaction

**Function**: `handler(event, lambda_context)` (the invocation log at line 272).

1. **Add a small helper** near the top of the module:
   ```python
   def _safe_event_metadata(event):
       """Return only non-sensitive authorizer-event fields for logging.
       NEVER include ``authorizationToken`` or ``headers`` (bearer tokens)."""
       metadata = {"methodArn": event.get("methodArn")}
       request_context = event.get("requestContext")
       if isinstance(request_context, dict) and request_context.get("requestId"):
           metadata["requestId"] = request_context["requestId"]
       return metadata
   ```
2. **Replace the sink line** (the current
   `logger.info(f"JWT Authorizer invoked with event: {json.dumps(event, default=str)}")`)
   with a redacted log of the small metadata dict only, e.g.:
   ```python
   logger.info("JWT Authorizer invoked: %s", _safe_event_metadata(event))
   ```
   The event is **never** logged wholesale; `authorizationToken` and `headers` are
   never referenced by the log path. (Using `%s` lazy-formatting is preferred but a
   redacted f-string of the small dict is equally acceptable — the invariant is
   that no token-bearing field is included.)
3. **Leave every other statement unchanged**, including the existing
   `logger.info(f"Authorization successful for user: {user_id}")` (a non-secret
   subject id — fine) and the `generate_policy(...)` allow/deny output. The policy
   the handler returns for a given event is untouched (preservation, Req 3.1).
4. `json` may remain imported (still used elsewhere for downstream needs); do not
   remove imports that are otherwise referenced.

#### S2 — `src/edgemlsdk/src/test/longevity/deploy.py` (Req 2.2) — remove embedded credentials

**Function**: `main(args)` command-list construction in
`download_edgemlsdk_release_artifacts` (~lines 230–231) and the mqtt
`run_mqtt_longevity` command (~line 250).

1. **Remove the two credential-export entries** from
   `download_edgemlsdk_release_artifacts`:
   ```python
   f"export AWS_ACCESS_KEY_ID={credentials.access_key}",
   f"export AWS_SECRET_ACCESS_KEY={credentials.secret_key}",
   ```
   Delete both list elements entirely. Keep the adjacent
   `f"export AWS_DEFAULT_REGION={q_region}"` line (region is not a secret and is
   already `shlex.quote`'d by the sibling spec). The `aws s3` / `aws ecr` commands
   that follow resolve credentials from the instance-profile role via IMDSv2 (the
   instance is launched with `MetadataOptions={"HttpTokens": "required", ...}`).
2. **Remove the `-a`/`-s` fragment** from the mqtt `run_mqtt_longevity` command
   string — delete exactly ` -a {credentials.access_key} -s {credentials.secret_key}`
   from the trailing `bash /edgemlsdk/mqtt/run_mqtt_longevity.sh ...` invocation.
   Every other byte of that command string — including the sibling spec's
   `shlex.quote`'d `-l {q_longevity_hours} -r {q_region} -m {q_mqtt_endpoint}
   -n {q_payload_size}` fragments — is preserved unchanged.
3. **Confirm the instance profile grants the needed access.** The instance is
   created via `create_instance(credentials, iam_instance_profile_arn)` with
   `IamInstanceProfile={"Arn": iam_instance_profile_arn}` (arn:...:instance-profile/
   `iam_role_for_edgemlsdk_longevity_tests`). The commands from which we remove the
   exports need S3 (`aws s3 sync`/`cp` on `panorama-sdk-v2-artifacts` and
   `edgeml-sdk-longevity-tests`) and ECR (`aws ecr get-login-password`) access. The
   design assumption is that this role already grants that access (it is the role
   the instance runs under); **verify** the role policy covers those buckets and
   the ECR repo before merging (see the follow-up note in S2b below). If it does
   not, the fix is to widen the instance-profile role policy — **never** to
   re-embed keys.
4. **Keep the `credentials = session.get_credentials()` local.** It is still passed
   to `create_instance(credentials, ...)` and
   `run_commands_via_ssm_with_retry(instance_id, 3, ..., credentials)`, which use
   `credentials.access_key`/`secret_key`/`token` as **boto3 client kwargs** (the
   local caller's own credentials for the EC2/SSM control-plane calls — NOT a
   command-string sink). Those kwargs are legitimate and are preserved. Because
   `credentials` remains referenced, no dead-code cleanup is required; do not remove
   it. (Only if it became entirely unused would we remove it — it does not.)

##### S2b — run_mqtt_longevity.sh cross-file dependency (follow-up)

Dropping `-a`/`-s` from the deploy command means
`src/edgemlsdk/src/test/longevity/mqtt/run_mqtt_longevity.sh` no longer receives
`aws_access_key_id`/`aws_secret_access_key`. That script currently runs, inside
the container:
```bash
aws configure set aws_access_key_id ${aws_access_key_id}
aws configure set aws_secret_access_key ${aws_secret_access_key}
aws configure set region ${aws_region}
```
With the flags dropped, `${aws_access_key_id}`/`${aws_secret_access_key}` are empty,
and `aws configure set aws_access_key_id ""` would write an **empty static
credential** to the container's `~/.aws/credentials`, which takes precedence over
and **breaks** the IMDS instance-role chain. The correct outcome is for the script
and the containerized SDK/CLI to source credentials from the instance role via
IMDS (the Docker container on the EC2 host can reach `169.254.169.254`).

**Required companion change (document as a follow-up task, do NOT re-embed keys):**
guard the `aws configure set` calls so they run only when a non-empty value is
provided — e.g. `[ -n "${aws_access_key_id}" ] && aws configure set aws_access_key_id "${aws_access_key_id}"`
— and keep `aws configure set region ${aws_region}` (region is not a secret). This
lets the CLI fall back to the instance-role credential provider. This spec's S2
change is in `deploy.py`; the `run_mqtt_longevity.sh` guard is the documented
dependent follow-up needed for the mqtt path to keep working. If for any reason the
script strictly requires explicit keys, prefer sourcing them from the instance
metadata / environment the profile provides rather than embedding them in the
command string.

#### S3 — `edge-cv-portal/backend/functions/packaging.py` (Req 2.3) — reserved domain

Change the synthetic identity email literal (in the auto-triggered
`greengrass_event` `requestContext.authorizer.claims`) from
`'email': 'system@edgecv.com'` to `'email': 'system@example.com'` (RFC 2606
reserved). No other value changes; all packaging behavior is otherwise identical
(Req 3.3).

#### S4 — `README.md` (Req 2.4) — placeholder password

In the Cognito admin setup command, replace
`  --password YourSecurePassword1234! \` with
`  --password <YOUR_SECURE_PASSWORD> \`. Documentation-only; the command flags and
structure are otherwise unchanged (Req 3.4).

#### S5 — `edge-cv-portal/backend/functions/jwt_authorizer.py` (Req 2.5) — documented unverified pre-parse

**Function**: `validate_jwt_token(token)` (the unverified decode at line 131).

1. Add a documented `# nosem` marker and comment on the unverified-decode line,
   e.g.:
   ```python
   # Unverified pre-parse: read `kid`/`iss` ONLY to select the JWKS key. The token
   # is NOT trusted here — a full RS256 signature-verified decode (verify_exp /
   # verify_aud / verify_iss) is enforced below at the `jwt.decode(token,
   # public_key, algorithms=['RS256'], ...)` call before any claim is used.
   unverified_payload = jwt.decode(  # nosem: jwt-python-none-alg / unverified-jwt-decode
       token, options={"verify_signature": False}
   )
   ```
   Use the exact rule id Semgrep reports for this finding; the comment records the
   routing-only intent and the enforced downstream verification.
2. **No control-flow change.** The two-stage decode is unchanged: the unverified
   pre-parse still reads `kid`/`iss`, and the verified decode at ~line 178 still
   enforces the signature and claims. A valid token still validates; a tampered
   token is still rejected by the verified decode (preservation, Req 3.5; and
   Fix-Checking asserts the tampered-token rejection).

#### S6 — `src/backend/app.py` (Req 2.6) — documented LAN-UI bind

On both `uvicorn.Config(app, host="0.0.0.0", ...)` lines (271 and 274) add a
`# nosem`/`# nosec` marker + rationale, e.g.:
```python
# nosec B104 — intentional LAN bind for an on-device edge appliance operator UI.
# Port 5443 is TLS-protected AND gated by station authorization; the plaintext
# 5000 path is reachable ONLY when station authorization is disabled.
config = uvicorn.Config(app, host="0.0.0.0", port=5443, ...)  # nosem: ...
```
The rationale on the 5443 line notes TLS + station auth; the rationale on the 5000
line notes it is the auth-disabled-only plaintext path. No bind behavior changes
(preservation, Req 3.6).

#### S7 — `edge-cv-portal/backend/functions/components.py` (Req 2.7) — B105 false positive

On the `pagination_token = ''` line (305) add a `# nosec B105` + one-line rationale,
e.g. `pagination_token = ''  # nosec B105 — empty pagination cursor, not a secret.`
Value and pagination behavior unchanged (Req 3.7).

#### S8 — `src/edgemlsdk/src/test/longevity/deploy.py` (Req 2.8) — B105 false positive

On the `secret_name = "edgeml-sdk-longevity-tests"` line (~59) add
`# nosec B105 — Secrets Manager secret name / S3 bucket name, not a password.`
Apply the same annotation to the `bucket_name = 'edgeml-sdk-longevity-tests'`
literal in `main` (~line 207) reported by the scanner. Values and their S3 /
Secrets Manager operations unchanged (Req 3.8).

#### S9 — `test/backend-test/utils/test_auth.py` (Req 2.9) — B106 false positives

On each of the six `token=` argument lines (47, 60, 71, 83, 95, 106 — the `token=""`,
`token="good-token"`, `token="inactive-token"`, and `token="some-token"` fixtures)
add a `# nosec B106` + rationale, e.g.
`auth.validate_token(token="good-token")  # nosec B106 — test-only fixture, not a real credential.`
Test inputs, assertions, and outcomes are unchanged; the suite still passes (Req 3.9).

#### S10 — Repo audit gate (Req 2.10)

Add a companion audit module and wire it into the same CI gate (see Testing
Strategy → Repo-audit design). It greps the in-scope files for the three
bug-condition sinks and asserts zero disallowed hits after the fix (minus
documented `# nosem`/`# nosec` exceptions).

### Ordering and risk

1. **Doc / `# nosem`-only changes first (S3, S4, S5, S6, S7, S8, S9)** — zero
   runtime risk (comment / literal-text edits, no control-flow change). Land and
   confirm the auth test suite still passes; preservation is "runtime behavior
   byte-for-byte identical".
2. **S1 (log redaction)** — substantive but low blast radius: only the invocation
   log line changes; the emitted IAM policy is untouched. Add the
   `_safe_event_metadata` helper and property-test that the log never contains a
   secret while the policy is unchanged.
3. **S2 (credential removal) LAST — highest risk** — it changes what `deploy.py`
   sends to the instance and shifts credential sourcing to the IAM instance
   profile, with a cross-file dependency on `run_mqtt_longevity.sh` (S2b). Apply
   the strongest preservation checks here: assert the constructed command strings
   equal the baseline **minus exactly** the removed credential fragments (byte-for-
   byte, including the sibling spec's `shlex.quote`'d args), and verify the
   instance-profile role grants the needed S3/ECR access before merging.
4. **Repo audit (S10)** last, as the gate that proves no disallowed sink remains.

**Highest-risk areas to watch:** the `deploy.py` credential removal (S2) — the
instance-profile role must actually grant S3/ECR/IoT access, and the
`run_mqtt_longevity.sh` `aws configure set` guard must be in place, or the mqtt
longevity path breaks; and the S1 log change — it must not accidentally drop the
non-secret operational signal (`methodArn`, `requestId`) that operators rely on.

## Testing Strategy

### Validation Approach

Two phases: first surface counterexamples that demonstrate the secret sinks /
undocumented patterns on the **unfixed** tree (repo audit + targeted tests), then
verify the fix **closes / neutralizes / documents** every buggy input (Fix
Checking) and **preserves** behavior for every legitimate input (Preservation
Checking, `F(X) = F'(X)`). Property-based testing (Hypothesis — the repo already
vendors `.hypothesis/`) is emphasized wherever the input domain is generatable:
the JWT authorizer (generated events with secret-bearing fields) and the deploy
SSM command construction (generated valid args).

### Repo-audit design (Req 2.10)

**Decision: add a companion `test/backend-test/security/secrets_audit.py` rather
than extend the sibling `repo_audit.py`.** Rationale (least-duplicative option):

- The two gates own different patterns (this one: full-event logging, credential
  interpolation, `verify_signature=False`; the sibling: subprocess interpolation,
  SSM f-strings, unsafe deserializers) and different in-scope file sets. Editing
  the sibling's committed `repo_audit.py` (and its `IN_SCOPE_FILES` /
  `_is_disallowed` semantics) would entangle two specs' gates and risk regressing
  the already-green Group-1 gate.
- To avoid duplication, `secrets_audit.py` **imports the proven low-level helpers**
  from the sibling module — `REPO_ROOT`, `EXCLUDE_DIRS`, `EXCLUDED_PATH_SUBSTRING`,
  `Hit`, `_grep`, `_parse_line`, `_has_nosem`, `_is_comment_line`, `_is_in_scope`
  (or a thin re-implementation if import coupling is undesirable) — and defines
  only its own `AUDIT_PATTERNS`, `IN_SCOPE_FILES`, and `_is_disallowed`. This
  mirrors `repo_audit.py`'s two-layer shape: a raw `run_audit()` broad enumeration
  (non-empty on the unfixed tree, used by the exploration test) and a precise
  `disallowed_hits()` gate (zero after fix, minus documented exceptions).

**In-scope files** (`IN_SCOPE_FILES`, relative to `REPO_ROOT`) — the real source
paths this spec owns, excluding vendored/generated copies:
- `edge-cv-portal/backend/functions/jwt_authorizer.py` (S1, S5)
- `src/edgemlsdk/src/test/longevity/deploy.py` (S2, S8)
- `edge-cv-portal/backend/functions/packaging.py` (S3)
- `src/backend/app.py` (S6)
- `edge-cv-portal/backend/functions/components.py` (S7)
- `test/backend-test/utils/test_auth.py` (S9)

**Precise gate semantics** (per category; a hit is *disallowed* only when it is in
`IN_SCOPE_FILES`, is not a comment line, carries no `# nosem`/`# nosec`, and
matches the rule):
- **`log_event_dump`** — a logging call whose argument contains `json.dumps(event`:
  regex `logger\.(info|debug|warning|error|critical)\(.*json\.dumps\(\s*event\b`.
  Disallowed when present without a documented marker. After S1 this is gone.
- **`cred_in_command`** — `access_key`/`secret_key` interpolated into a command /
  list string: disallowed when a line references `\.(access_key|secret_key)\b`
  (or `access_key`/`secret_key` inside `{...}`) **AND** the reference sits inside a
  string literal being built for a command — detected as an f-string on the line
  (`\b[rRbB]?f['\"]`) or `%`/`.format(`/`+` interpolation around it. The boto3
  client **kwargs** `aws_access_key_id=credentials.access_key` (a bare keyword
  argument, no surrounding string literal) do NOT match and are allowed. After S2
  the two `export ...` list entries and the `-a`/`-s` f-string fragment are gone.
- **`unverified_jwt`** — `verify_signature` set to `False`:
  regex `verify_signature['\"]?\s*[:=]\s*False`. Disallowed when present **without**
  a documented `# nosem` on the line. After S5 the pre-parse line carries the
  documented marker, so it is allowed.

**Scoping precision** (mirroring the sibling gate): the gate is asserted only over
`IN_SCOPE_FILES`, so it does NOT match the security test/fixture files' own pattern
strings, the vendored `src/backend/edgemlsdk/edgemlsdk/` duplicate of `deploy.py`,
the generated `cdk.out/asset.*` artifacts, or files owned by other specs. Precision
+ this scoping (not a hard-coded line list) is what lets the gate still FAIL if a
full-event log, an interpolated credential, or an un-annotated
`verify_signature=False` is reintroduced into any real fixed source file.

**CI wiring:** add the new gate next to the existing Group-1 gate in
`build-custom.sh` (the "Security … audit gate" block, ~lines 207–225), under
`set -e`, so a non-zero exit fails the build:
```sh
echo "Running security secrets/credentials/JWT audit gate..."
python${PYTHON_VERSION} test/backend-test/security/secrets_audit.py
python${PYTHON_VERSION} -m pytest \
  test/backend-test/security/test_secrets_bug_condition_exploration.py -v
python${PYTHON_VERSION} -m pytest \
  test/backend-test/security/preservation -p no:cacheprovider --noconftest -v
```
(The exploration test file mirrors the sibling's
`test_bug_condition_exploration.py`; preservation tests may live alongside the
existing `security/preservation` suite.)

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate each sink / undocumented pattern
BEFORE the fix and confirm/refute the root-cause analysis. If refuted,
re-hypothesize.

**Test Plan**: Run `secrets_audit.run_audit()` to enumerate every hit across the
in-scope files, and add targeted tests that observe the secret reaching the sink on
unfixed code.

**Test Cases**:
1. **JWT full-event log (S1)**: call `handler` with an event carrying a
   `authorizationToken`/`headers` bearer token and capture the log record; assert
   the emitted log line **contains** the token substring on unfixed code
   (counterexample) — after fix it must NOT.
2. **deploy.py credential interpolation (S2)**: build the command lists with a
   stubbed `credentials` (fake access/secret keys) and assert the fake key
   **appears** in the constructed `download_edgemlsdk_release_artifacts` /
   `run_mqtt_longevity` strings on unfixed code (counterexample) — after fix it
   must NOT.
3. **Unverified decode without marker (S5)**: assert the `verify_signature: False`
   line carries **no** `# nosem` on unfixed code (audit hit) — after fix it does.
4. **Repo audit (S10)**: `secrets_audit.run_audit()` returns non-empty hits across
   S1/S2/S5 (and enumerates S6–S9 undocumented markers) on the unfixed tree; every
   such hit is a counterexample where `isBugCondition` is true.

**Repo-audit grep patterns** (must be non-empty on unfixed, zero disallowed hits
after fix — minus documented exceptions), scoped to `IN_SCOPE_FILES`, excluding
`cdk.out/asset.*` and the vendored `edgemlsdk/edgemlsdk/` duplicate:
- Full-event logging: `logger\.(info|debug|warning|error|critical)\(.*json\.dumps\(\s*event\b`.
- Credential interpolation into a command/list string:
  `\.(access_key|secret_key)\b` (or `{...access_key...}` / `{...secret_key...}`)
  occurring inside an f-string / `%` / `.format(` / `+`-built string (NOT a bare
  boto3 kwarg).
- Unverified JWT decode: `verify_signature['\"]?\s*[:=]\s*False` **without** a
  documented `# nosem` on the line.
- (Enumeration-only, for the exploration report) undocumented `0.0.0.0` bind
  (`host\s*=\s*['\"]0\.0\.0\.0['\"]` without `# nosem`/`# nosec`) and B105/B106
  literals — these are surfaced by the raw enumeration but the precise gate treats
  them as documented once S6–S9 land.

**Expected Counterexamples**:
- Non-empty audit hits across S1, S2, S5 (and the S6–S9 undocumented markers).
- The bearer token appears verbatim in the S1 log line.
- The fake access/secret key appears verbatim in the S2 command strings.

### Fix Checking

**Goal**: For all inputs where the bug condition holds, the fixed code neutralizes
the sink / documents the pattern.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := fixedFunction(input)
  ASSERT neutralized(result)
END FOR
```

Concretely:
- S1: generated authorizer events with arbitrary token/header values →
  `_safe_event_metadata` + the redacted log call produce a log line that contains
  `methodArn`/`requestId` and **never** the token or headers (Req 2.1).
- S2: with a stubbed `credentials`, the constructed SSM command strings contain
  **no** `access_key`/`secret_key` value and no `export AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` / `-a`/`-s` fragment (Req 2.2).
- S3/S4: the email is `system@example.com`; the README password is
  `<YOUR_SECURE_PASSWORD>` (Req 2.3, 2.4).
- S5: a **tampered** token is still rejected by the verified decode; the pre-parse
  line carries the documented `# nosem` (Req 2.5).
- S6–S9: each flagged line carries a documented `# nosem`/`# nosec` + rationale
  (Req 2.6–2.9).
- S10: post-fix `secrets_audit.disallowed_hits()` returns zero (Req 2.10).

### Preservation Checking

**Goal**: For all inputs where the bug condition does NOT hold, the fixed code
produces the same result as the original — `F(X) = F'(X)`.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT originalFunction(input) = fixedFunction(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended where the domain is
generatable (authorizer events, deploy args); capture baseline behavior on the
**unfixed** code first, then assert the fixed code matches.

**Property-based test plans:**
- **S1 JWT authorizer — policy invariance**: generate authorizer events with
  random `methodArn`, random secret-bearing `authorizationToken`/`headers`, and
  random claims (mock `validate_jwt_token` / JWKS so the decode is deterministic).
  Invariants: (a) the returned IAM policy (`principalId`, `Effect`, `Resource`,
  `context`) is **identical** to `F`'s for the same event; (b) the captured log
  output **never** contains the token or any `headers` value — only `methodArn`
  and, when present, `requestId`.
- **S2 deploy.py — command-string equivalence**: generate valid arg tuples
  (platform/ubuntu/python/region/date/mqtt-endpoint shapes that pass the sibling
  spec's allowlist) with a stubbed `credentials`. Invariant: the constructed
  `download_edgemlsdk_release_artifacts` and `run_mqtt_longevity` strings equal the
  **baseline `F` strings with exactly the removed fragments deleted** — i.e.
  `F'` == `F` minus `export AWS_ACCESS_KEY_ID=...`, minus `export
  AWS_SECRET_ACCESS_KEY=...`, minus ` -a {access_key} -s {secret_key}`; every other
  byte (including the `shlex.quote`'d `-l/-r/-m/-n` fragments) is identical.

**Example-based preservation cases:**
1. **S5 JWT two-stage decode**: a valid token still validates to the same claims;
   a tampered token still raises `AuthorizationError` (Req 3.5).
2. **S6 bind**: with authorization enabled the config binds `0.0.0.0:5443` with TLS;
   disabled → `0.0.0.0:5000` plaintext — unchanged (Req 3.6).
3. **S7 pagination**: `list_private_components` still initializes and pages with the
   empty `pagination_token` cursor (Req 3.7).
4. **S8 bucket/secret name**: `set_aws_access_keys_from_secrets_manager` and the S3
   uploads still use `'edgeml-sdk-longevity-tests'` (Req 3.8).
5. **S9 auth suite**: the existing `test_auth.py` cases pass unchanged (Req 3.9).
6. **Out-of-scope untouched**: the vendored `requests/auth.py` / `urllib3` findings,
   the `cdk.out/asset.*` copies, and the vendored `edgemlsdk/edgemlsdk/` duplicate
   are unchanged (Req 3.10).

### Unit Tests

- S1: `_safe_event_metadata` returns exactly `{methodArn[, requestId]}` and omits
  `authorizationToken`/`headers`; the handler log line for a token-bearing event
  contains no secret; the allow and deny policies are unchanged for allow/deny
  cases.
- S2: canonical valid args produce command strings equal to the baseline minus the
  credential fragments; assert no `access_key`/`secret_key`/`AWS_ACCESS_KEY_ID`/
  `AWS_SECRET_ACCESS_KEY` substring remains; assert the boto3 client kwargs in
  `create_instance`/`run_commands_via_ssm_with_retry` still receive the credentials.
- S3/S4: literal assertions on the email value and the README placeholder.
- S5: tampered-token rejection; presence of the documented `# nosem` on the
  pre-parse line.
- S6–S9: presence of the documented `# nosem`/`# nosec` + rationale on each flagged
  line; unchanged runtime behavior (bind config, pagination, bucket value, auth
  fixtures).
- S10: `secrets_audit.disallowed_hits()` == `[]`; `run_audit()` excludes
  `cdk.out/asset.*` and the vendored duplicate.

### Property-Based Tests

- S1: generate authorizer events (random arns, secret tokens/headers, claims) →
  invariant: policy identical to `F` AND log line free of any secret.
- S2: generate valid arg tuples → invariant: constructed command strings equal the
  baseline minus exactly the removed credential fragments; no key substring present.

### Integration Tests

- S1: invoke the authorizer Lambda end-to-end with a valid token → same allow
  policy returned, CloudWatch log line free of the token; with an invalid token →
  deny policy, no token logged.
- S2: (in a test/staging account) run `deploy.py` against an instance launched with
  the instance profile → the SSM commands complete their S3/ECR steps using
  instance-role credentials, and the SSM command history / CloudWatch logs contain
  no access/secret key.
- S10: run `secrets_audit.py` in CI (the `build-custom.sh` gate) — it fails if any
  disallowed sink reappears in in-scope application code.
