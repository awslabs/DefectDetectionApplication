# Implementation Plan

## Overview

This plan follows the bug-condition methodology. Any application-code path that
carries an untrusted / argument-controlled input into a shell command or an
unsafe deserializer (`pickle` / `dill` / `torch.load`) is the bug
(`isBugCondition(X)` true); the fix **neutralizes** every one of those eight
sites — malicious input is rejected or rendered inert — while preserving the
behavior for every legitimate input byte-for-byte (`F(X) = F'(X)`).

- **Property 1: Fix Checking** — for all inputs where `isBugCondition` is true,
  the fixed code neutralizes the vector (injection rejected/quoted/operand-only;
  crafted payload cannot execute code) and the repo audit returns zero disallowed
  hits (Requirements 2.1–2.9).
- **Property 2: Preservation** — for all inputs where `isBugCondition` is false,
  `F(X) = F'(X)` (Requirements 3.1–3.7).

Finding traceability to the scan (real source paths only; the
`edge-cv-portal/infrastructure/cdk.out/asset.*` copies are generated and out of
scope):

- **#1** `src/backend/snapshot/Snapshotter.py` — `stationName` → shell script
- **#2** `src/edgemlsdk/src/test/longevity/deploy.py` — argparse args → SSM `AWS-RunShellScript`
- **#3** `src/backend/utils/utils.py` (+ `user_group_management_utils.py`, `filesystem_management_utils.py`) — `run_command`
- **#4** `test/backend-test/host_scripts/test_docker_profile_selection.py` — `bash -c` audit (nosem)
- **#5** `src/backend/lyra_science_processing_utils/model_processors/supervised_bbox_stage1_postprocessor.py` — `dill.load`
- **#6** `src/backend/utils/camera_manager.py` — `pickle.loads` (camera frame)
- **#7** `src/backend/utils/digital_input_process_manager.py` — `pickle.loads` (DIO health message)
- **#8** `edge-cv-portal/backend/functions/model_converter.py` — `torch.load`

## Tasks

- [x] 1. Write bug-condition exploration test (repo audit + targeted exploit-shaped tests)
  - **Property 1: Bug Condition** - Untrusted / argument-controlled input reaches a shell command or an unsafe deserializer across eight application-code sites
  - **CRITICAL**: This test MUST FAIL (surface non-empty hits / fire the sentinels) on the unfixed tree - the hits ARE the counterexamples that confirm the bug exists
  - **DO NOT attempt to fix any application source code in this task** - this task only writes tests and documents the counterexamples
  - **NOTE**: This same audit + exploit set becomes the fix-checking assertion in task 12 (it must return zero disallowed hits / neutralize every payload after the fix)
  - **GOAL**: Enumerate every bug-condition site and demonstrate each vector so the fix scope is grounded in real code
  - **Scoped PBT Approach**: The audit is deterministic (scope it to a concrete, reproducible grep over the known in-scope tree); the exploit-shaped tests use Hypothesis (already vendored under `.hypothesis/`) where the input domain is generatable (metacharacter/option-injection strings, crafted payloads), scoped to concrete failing shapes for reproducibility
  - **Repo audit (finding #9 / Req 2.9)** — write an audit script/test that runs `grep -rn` for the bug-condition patterns from the design, scoped to in-scope application code and EXCLUDING `edge-cv-portal/infrastructure/cdk.out/asset.*`:
    - Shell/subprocess interpolation: `subprocess\.(run|call|check_output|check_call|Popen)` combined with f-string / `%` / `.format(` / `+`-built command args, and any `shell=True`
    - SSM shell docs: `AWS-RunShellScript` with f-string-built `commands`
    - Deserializers: `\bpickle\b`, `\bdill\b`, `pickle\.loads?`, `dill\.loads?`, `torch\.load\(` (flag any without `weights_only=True`)
  - **Targeted exploit-shaped tests** (assert the vector reaches the sink on UNFIXED code):
    - **#1** `src/backend/snapshot/Snapshotter.py`: call `take_snapshot("a; touch /tmp/pwn")` and assert the metacharacter payload reaches the `path` argument passed to `subprocess.check_output(["sh", "/snapshot/snapshot.sh", path])`
    - **#2** `src/edgemlsdk/src/test/longevity/deploy.py`: build the command lists with `--platform "x; touch /tmp/pwn"` (and other args) and assert the raw payload appears UNQUOTED in the constructed `AWS-RunShellScript` command string
    - **#3** `src/backend/utils/utils.py` callers: call `create_user("-oroot")` / `chmod("/x", "-x")` and assert the leading-`-` value is passed as a bare operand with NO `--` guard (option injection reaches the tool)
    - **#5** `.../supervised_bbox_stage1_postprocessor.py`, **#6** `src/backend/utils/camera_manager.py`, **#7** `src/backend/utils/digital_input_process_manager.py`, **#8** `edge-cv-portal/backend/functions/model_converter.py`: construct a crafted payload whose `__reduce__` sets a sentinel (writes a temp file / flips a flag), load it via each site's deserializer (`dill.load` / `pickle.loads` / `torch.load`; use a crafted `.pt` for #8), and assert the sentinel FIRES (demonstrates code execution)
  - Run the audit and exploit tests on the UNFIXED tree
  - **EXPECTED OUTCOME**: Audit returns NON-EMPTY hits across the eight sites AND each exploit test surfaces its counterexample (metacharacter/option-injection reaches the command; the deserialization sentinel fires) - this is correct, it proves the bug exists
  - Document the counterexamples found per finding (e.g. `Snapshotter.take_snapshot("a; touch /tmp/pwn")` → payload in `path`; `deploy.py --platform "x; touch /tmp/pwn"` → unquoted in SSM command; `create_user("-oroot")` → bare operand; `dill.load`/`pickle.loads`/`torch.load` sentinel fired)
  - Mark task complete when the audit + exploit tests are written, run, and the counterexamples are documented
  - _Requirements: 1.1, 1.2, 1.3, 1.5, 1.6, 1.7, 1.8_

- [x] 2. Write preservation baseline tests on the UNFIXED code (BEFORE implementing any fix)
  - **Property 2: Preservation** - No behavior change for legitimate (non-bug-condition) inputs
  - **IMPORTANT**: Follow observation-first methodology - capture `F(X)` baselines on the UNFIXED tree, then (in task 13) assert the fixed code `F'(X)` matches exactly
  - **Emphasize property-based tests** (Hypothesis, already vendored under `.hypothesis/`) wherever the input domain is generatable
  - Observe and record baselines on unfixed code:
    - **#1** `src/backend/snapshot/Snapshotter.py`: for valid `^[a-zA-Z0-9_-]+$` `stationName` (timestamp pinned/mocked), record the constructed `snapshot-<name>-<ts>.tar` path and the returned `"snapshotfile/<file>.gz"` string (Req 3.1)
    - **#2** `src/edgemlsdk/src/test/longevity/deploy.py`: for legitimate args (valid platform / ubuntu+python versions / region / MQTT endpoint / 8-digit release date / numeric sizes), record the exact constructed SSM `AWS-RunShellScript` command strings (Req 3.2)
    - **#3** `src/backend/utils/utils.py` + `user_group_management_utils.py` / `filesystem_management_utils.py`: for valid usernames/groupnames/paths/modes, record the EXACT `argv` passed to `subprocess.run` (mock it) and the `(success, output)` return contract for `create_user`/`delete_user`/`create_group`/`delete_group`/`add_user_to_group`/`remove_user_from_group` and `chmod`/`chown`/`chgrp` (with and without `-R`) (Req 3.3)
    - **#4** `test/backend-test/host_scripts/test_docker_profile_selection.py`: run the existing suite; record that the `tegra`/`generic` decision assertions and regression guards pass (Req 3.4)
    - **#5** `.../supervised_bbox_stage1_postprocessor.py`: for a legitimate reference-image map, record the resulting `train_feature_gallery` (`np.vstack`) and the ordered `reference_image_paths` (Req 3.5)
    - **#6** `src/backend/utils/camera_manager.py`: for a legitimate frame, record the round-tripped `{'data','height','width'}` dict (and `None` on timeout/failure) (Req 3.5)
    - **#7** `src/backend/utils/digital_input_process_manager.py`: for a legitimate health message, record the round-tripped `{'status','error_type','last_updated'}` dict (Req 3.5)
    - **#8** `edge-cv-portal/backend/functions/model_converter.py`: for legitimate `.pt` files (raw state dict, checkpoint, JIT model, full model), record the inspected metadata and the generated DDA package (Req 3.6)
    - **Out-of-scope guard**: record the exact bytes of the `cdk.out/asset.*` copies and the embedded-credential lines in `deploy.py` so task 13 can assert they are unchanged (Req 3.7)
  - Write tests that assert the recorded baselines. Use **property-based tests** where the domain is generatable (per the design's Testing Strategy):
    - **#1**: generate valid `^[a-zA-Z0-9_-]+$` names; invariant — same path + same `"snapshotfile/<file>.gz"` return as `F`
    - **#2**: generate valid argument tuples (platform/version/region/date shapes); invariant — constructed SSM command strings equal `F`'s
    - **#3**: generate valid username/groupname/path/mode values; invariant — exact `F` `argv` and `(success, output)` contract
    - **#5/#7** (and **#6** if format-changed): generate valid payload structures; invariant — safe-format round-trip equals the pickle/dill round-trip of `F`
  - Run the tests on the UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this captures the baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

- [x] 3. Fix #1 — Snapshotter: allowlist-validate + pathlib-constrain `stationName` (pure-validation, lowest risk)
  - [x] 3.1 Harden `take_snapshot(stationName)` in `src/backend/snapshot/Snapshotter.py`
    - Allowlist-validate `stationName` against `^[a-zA-Z0-9_-]+$` at the top of the function; on mismatch (or empty) raise `HTTPException(status_code=400, detail=...)` before constructing the path or calling `subprocess`
    - Defense-in-depth: build the filename, resolve the full path with `pathlib` (`Path("/aws_dda/system") / file`), and assert `resolved.parent == Path("/aws_dda/system")` so no `../` or absolute-path escape is possible
    - Leave the `subprocess.check_output(["sh", "/snapshot/snapshot.sh", path])` call and the `"snapshotfile/" + file + ".gz"` return UNCHANGED for valid names (timestamp component unchanged)
    - _Bug_Condition: isBugCondition(X) where X = stationName reaching the shell script via concatenated path (#1)_
    - _Expected_Behavior: metacharacter names rejected with HTTP 400 and/or path constrained to /aws_dda/system/; no metacharacter reaches the shell as executable syntax_
    - _Preservation: valid ^[a-zA-Z0-9_-]+$ names produce the same snapshot-<name>-<ts>.tar path and the same "snapshotfile/<file>.gz" return (Req 3.1)_
    - _Requirements: 2.1_

- [x] 4. Fix #2 — deploy.py: allowlist-validate + shlex.quote the SSM args (pure-validation, low risk)
  - [x] 4.1 Harden argument handling in `src/edgemlsdk/src/test/longevity/deploy.py`
    - Allowlist-validate each interpolated arg as it is read: `platform`, `ubuntu_version`, `python_version`, `region`, `mqtt_endpoint` against conservative patterns (e.g. `^[A-Za-z0-9._:-]+$`), `release_date` against `^\d{8}$`; keep `longevity_hours`/`payload_size` as argparse `int`. Reject on mismatch (argparse `error()` / `ValueError`)
    - Wrap every interpolated value in `shlex.quote(str(value))` when building each command string in `download_edgemlsdk_release_artifacts` and `run_mqtt_longevity`
    - Do NOT alter the embedded `credentials.access_key`/`secret_key` handling (separate remediation group); quoting them is acceptable but do not otherwise change them
    - _Bug_Condition: isBugCondition(X) where X = argparse args f-string-interpolated into AWS-RunShellScript commands (#2)_
    - _Expected_Behavior: metacharacter args are shlex.quote'd and/or allowlist-rejected; constructed SSM commands contain no live metacharacter_
    - _Preservation: valid args (aarch64, 22.04, 3.11, us-west-2, 8-digit date, numeric sizes) produce equivalent SSM command strings — shlex.quote is a no-op on clean tokens (Req 3.2)_
    - _Requirements: 2.2_

- [x] 5. Fix #3 — utils.run_command callers: `--` sentinel + allowlist, preserve exact argv (pure-validation, low risk)
  - [x] 5.1 Document `run_command` and harden its callers in `src/backend/utils/utils.py`, `user_group_management_utils.py`, `filesystem_management_utils.py`
    - Keep `run_command` in its arg-list, no-`shell=True` form; add a `# nosem`/comment documenting that it never uses a shell and that operand validation is enforced at the callers
    - Neutralize option injection at the callers with a `--` end-of-options sentinel, placing user-influenced operands after `--`:
      - `create_user`: `['useradd', ..., '--', username]`; `delete_user`: `['userdel', '--', username]`
      - `create_group`: `['groupadd', ..., '--', groupname]`; `delete_group`: `['groupdel', '--', groupname]`
      - `add_user_to_group` / `remove_user_from_group`: keep the `gpasswd -a`/`-d` flag, apply `--` before trailing operands as the tool allows
      - `chmod` / `chown` / `chgrp` (+ `-R` variants): `['chmod', mode, '--', path]`, `['chown', owner, '--', path]`, `['chgrp', groupname, '--', path]`
    - Allowlist-validate the identity operands a `--` sentinel does not fully cover: reject `username`/`groupname` not matching `^[a-z_][a-z0-9_-]*$`, and reject a `mode` not matching a symbolic/octal `chmod` pattern; raise a clear error on rejection
    - _Bug_Condition: isBugCondition(X) where X = username/groupname/path/mode reaching subprocess.run as an option-injection operand (#3)_
    - _Expected_Behavior: leading-`-` / metacharacter operands rejected by allowlist or placed after `--` so the tool treats them as operands; still no shell=True, still arg-list form_
    - _Preservation: valid operands produce a semantically identical argv (post-`--` tokens are the same operands) and the same (success, output) contract; tests assert the exact argv (Req 3.3)_
    - _Requirements: 2.3_

- [x] 6. Fix #4 — document the test subprocess audit finding (nosem, lowest risk)
  - [x] 6.1 Add a documented `# nosem` justification in `test/backend-test/host_scripts/test_docker_profile_selection.py`
    - Add `# nosem: <rule-id>` (and a short comment) on the `subprocess.run(["bash", "-c", snippet], ...)` line recording that `snippet` is derived from STATIC, in-repo `.sh` content, is test-only, and carries no untrusted input
    - No behavioral change
    - _Bug_Condition: isBugCondition(X) where X = static in-repo snippet flagged by the subprocess audit (#4)_
    - _Expected_Behavior: the audit finding is resolved with a recorded rationale (documented exception)_
    - _Preservation: the tests continue to exercise the real decision block; tegra/generic assertions and regression guards pass unchanged (Req 3.4)_
    - _Requirements: 2.4_

- [x] 7. Fix #7 — DIO health message: replace pickle with framed JSON over the documented process-local boundary (smallest faithful format change)
  - [x] 7.1 Serialize the health message as framed JSON in `src/backend/utils/digital_input_process_manager.py`
    - In `__update_health_status`: encode `status` (a `DIOProcessHealthStatusEnum`) as its value/name, `error_type` as its string form, `last_updated` as the float timestamp; write a 4-byte length header then the UTF-8 JSON into the `shared_memory.SharedMemory` block (well-defined framing instead of `buf[:len(pickled)]` / whole-`buf` read)
    - In `get_dio_process_health_report`: parse the framed JSON with `json.loads`, reconstruct the identical dict shape (map `status` back to the enum)
    - Document the process-local trust boundary (`dda_dio_mem_block_process_<workflow_id>`) in-code as defense-in-depth
    - _Bug_Condition: isBugCondition(X) where X = shared-memory buffer bytes reaching pickle.loads (#7)_
    - _Expected_Behavior: no pickle deserializer on this path; the health message is JSON-framed and cannot execute code_
    - _Preservation: legitimate messages still yield the identical {'status','error_type','last_updated'} dict for all callers (Req 3.5)_
    - _Requirements: 2.7_

- [x] 8. Fix #5 — reference-image map: replace dill with JSON+numpy plus a migration shim (simple payload, config-driven → highest deser risk)
  - [x] 8.1 Replace `dill.load` with a safe format in `src/backend/lyra_science_processing_utils/model_processors/supervised_bbox_stage1_postprocessor.py`
    - Define a safe on-disk format: a JSON sidecar for the `path` keys plus a `.npz`/`.npy` for the stacked feature vectors (or a single `.npz` with an object array of paths + a float matrix); load with `numpy.load(..., allow_pickle=False)` and `json.load`
    - Preserve the exact in-memory result: `self.train_feature_gallery = np.vstack(train_feature_gallery)` and the ordered `self.reference_image_paths` identical to what `dill.load` produced (downstream nearest-neighbor lookup unchanged)
    - Provide a one-time migration/loader shim: if only a legacy `dill` map exists, convert it ONCE behind an explicit, documented trusted-conversion path (or an offline utility) and write the safe format; the runtime `__init__` load path uses ONLY the safe format — do not `dill.load` externally-supplied files at inference time
    - _Bug_Condition: isBugCondition(X) where X = config-driven reference_image_map_file reaching dill.load (#5)_
    - _Expected_Behavior: runtime load uses JSON+numpy (allow_pickle=False); a crafted file cannot execute code_
    - _Preservation: a legitimate reference map yields the identical train_feature_gallery + ordered reference_image_paths (Req 3.5)_
    - _Requirements: 2.5_

- [x] 9. Fix #6 — camera frame: prefer a format change, with a documented+enforced trust-boundary fallback (highest preservation risk, live hot path)
  - [x] 9.1 Harden the frame transport in `src/backend/utils/camera_manager.py`
    - **Preferred (format change):** replace the pickle transport with a non-executable serialization of the fixed `{'data','height','width'}` shape — e.g. a small struct/JSON header (`height`, `width`) plus raw `data` bytes, or `numpy` `tobytes()`/`frombuffer`; keep the exact dict shape so the former `pickle.loads` callers become the new decoder with identical output
    - **Fallback (only if the `BaseManager` proxy contract makes a format change impractical):** add an in-code comment documenting the enforced in-process trust boundary (producer and consumer are the same `BaseManager`-hosted `Camera`; no untrusted bytes reach this path) plus a `# nosem: <rule-id>`, and ensure the load site cannot be reached by externally-supplied bytes
    - Preserve the null/timeout paths (`pickle.dumps(None)` → `None`) so the existing error/None-handling contract in `get_camera_frame` is unchanged
    - _Bug_Condition: isBugCondition(X) where X = frame payload bytes reaching pickle.loads (#6)_
    - _Expected_Behavior: the deserializer is eliminated (format change) OR confined to a documented+enforced in-process trust boundary the payload cannot cross_
    - _Preservation: legitimate frames yield the identical {'data','height','width'} dict and None on timeout/failure (Req 3.5)_
    - _Requirements: 2.6_

- [x] 10. Fix #8 — model_converter: `weights_only=True` + trusted-bucket allowlist + allowlisted-trusted-source fallback (must not regress legit checkpoints)
  - [x] 10.1 Harden the load path in `edge-cv-portal/backend/functions/model_converter.py`
    - Default `inspect_pytorch_model` to `torch.load(model_path, map_location='cpu', weights_only=True)` so a malicious `.pt` cannot execute code during load (primary neutralization)
    - In the `convert_model` request path: validate `model_s3_uri` against a config-driven allowlist of trusted buckets/accounts before download; reject anything else with a 400
    - Legitimate full-checkpoint fallback (preservation): attempt `weights_only=True` first; on the specific unpickling/`weights_only` failure, ONLY if the source is on the trusted-bucket/account allowlist, retry with `weights_only=False` under a documented "allowlisted-trusted-source" branch (comment + audit log); if not allowlisted, surface the error instead of loading executable pickle. Preserve the existing state-dict vs. checkpoint vs. JIT (`hasattr(model_data,'graph')`) vs. full-model (`is_full_model`) detection
    - Keep `inspect_pytorch_model`'s broad `except` → "Could not inspect model" contract so a rejected/failed load degrades exactly as today
    - _Bug_Condition: isBugCondition(X) where X = user-provided model_s3_uri .pt reaching torch.load without weights_only (#8)_
    - _Expected_Behavior: weights_only=True by default; source restricted to a trusted-bucket allowlist; weights_only=False reachable only for allowlisted trusted sources; a malicious .pt / non-allowlisted URI is rejected_
    - _Preservation: legitimate state-dict/checkpoint/JIT/full-model files yield the identical inspected metadata and generated DDA package, including full checkpoints via the allowlisted-trusted-source path (Req 3.6)_
    - _Requirements: 2.8_

- [x] 11. Fix #9 — repo audit gate (Req 2.9)
  - [x] 11.1 Finalize the repo-audit check as the pattern gate
    - Keep the audit from task 1 as a runnable check that greps the in-scope tree for the bug-condition patterns (subprocess shell-string interpolation / `AWS-RunShellScript` f-strings; `pickle`/`dill`/`torch.load` on externally-influenced data)
    - Assert zero disallowed hits in in-scope application code, allowing ONLY occurrences carrying a documented `# nosem`/comment exception (the #4 test line, and any #6 documented-boundary fallback)
    - Exclude `edge-cv-portal/infrastructure/cdk.out/asset.*` (generated artifacts)
    - _Bug_Condition: isBugCondition(X) where X = any remaining disallowed shell-interpolation / unsafe-deserializer occurrence in in-scope code (#9)_
    - _Expected_Behavior: audit returns zero disallowed hits, minus documented justified exceptions_
    - _Preservation: the generated CDK artifacts and out-of-scope findings are not touched (Req 3.7)_
    - _Requirements: 2.9_

- [x] 12. Verify the bug-condition exploration test now passes (Fix Checking)
  - **Property 1: Expected Behavior** - Every injection / deserialization vector neutralized
  - **IMPORTANT**: Re-run the SAME audit + exploit-shaped tests from task 1 - do NOT write new tests
  - Re-run the targeted exploit tests: #1 metacharacter `stationName` → HTTP 400 / no payload in `path`; #2 metacharacter args → quoted/rejected, no live metacharacter in the SSM command; #3 leading-`-` operands → rejected or placed after `--`; #5/#6/#7/#8 crafted payloads → sentinel does NOT fire (no code execution)
  - Re-run the repo audit over the full in-scope tree
  - **EXPECTED OUTCOME**: Every exploit payload is neutralized AND the audit returns ZERO disallowed hits (minus the documented `# nosem` exceptions for #4 and any #6 boundary fallback)
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9_

- [x] 13. Verify preservation baseline tests still pass (Preservation Checking)
  - **Property 2: Preservation** - No behavior change for legitimate inputs
  - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
  - Run the preservation baselines/property tests under the fix: #1 same snapshot path + return; #2 equivalent SSM command strings; #3 exact `argv` + `(success, output)`; #4 docker-profile decision/regression assertions; #5/#6/#7 identical round-trip structures (reference map, camera frame, DIO health message); #8 identical inspected metadata + generated package (including trusted-source full checkpoints)
  - Confirm the `cdk.out/asset.*` copies and the embedded-credential lines in `deploy.py` are unchanged
  - **EXPECTED OUTCOME**: Tests PASS (no regressions); `F(X) = F'(X)` for all non-bug-condition inputs
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

- [x] 14. Integration + CI-gate verification
  - [x] 14.1 Run the backend test suite and the repo-audit CI gate
    - Run the backend test suite (`test/backend-test`) to completion and confirm the injection/deserialization unit + property + integration tests pass and no regressions surface
    - Run the repo-audit check as a CI gate and confirm it fails on any reintroduced disallowed pattern in in-scope application code
    - End-to-end spot checks: snapshot request with a valid `stationName` returns the same gz path (malicious → HTTP 400); user/group + filesystem flows with valid inputs produce identical outcomes (option-injection rejected); postprocessor / camera preview-capture / DIO health report on legitimate data return identical results; a legitimate model (state dict + trusted-source full checkpoint) converts to the same package (malicious `.pt` / non-allowlisted URI rejected)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

- [x] 15. Checkpoint - Ensure all tests pass and wire the audit into CI
  - Confirm the task-1 audit + exploit tests now neutralize every vector and return zero disallowed hits (task 12), the task-2 preservation tests still pass (task 13), and the backend suite + integration checks pass (task 14)
  - Add the repo-audit check to CI so a disallowed subprocess-interpolation / unsafe-deserializer pattern reappearing in in-scope application code fails the build
  - Ensure all tests pass; ask the user if questions arise

---

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "description": "Run on UNFIXED code: surface injection/deserialization counterexamples (audit + exploit tests) and capture preservation baselines (independent).", "tasks": ["1", "2"] },
    { "wave": 2, "description": "Pure-validation fixes first (low risk, independently testable as pure functions of their inputs).", "tasks": ["3.1", "4.1", "5.1", "6.1"] },
    { "wave": 3, "description": "Deserialization format changes: #7 (smallest faithful change) then #5 (migration shim, simple payload).", "tasks": ["7.1", "8.1"] },
    { "wave": 4, "description": "Camera frame (#6): prefer format change, documented-boundary fallback — highest preservation risk (live hot path + BaseManager proxy).", "tasks": ["9.1"] },
    { "wave": 5, "description": "model_converter (#8): weights_only=True + trusted-source allowlist + allowlisted-fallback to preserve legit checkpoints.", "tasks": ["10.1"] },
    { "wave": 6, "description": "Repo-audit gate (#9) — the pattern gate that proves no disallowed occurrence remains.", "tasks": ["11.1"] },
    { "wave": 7, "description": "Fix Checking and Preservation Checking (re-run tasks 1 and 2 on fixed code).", "tasks": ["12", "13"] },
    { "wave": 8, "description": "Integration + CI-gate verification (backend suite + repo audit).", "tasks": ["14.1"] },
    { "wave": 9, "description": "Checkpoint: all green + wire the repo-audit check into CI.", "tasks": ["15"] }
  ]
}
```

Visual summary of the critical path:

```
1. Bug-condition audit + exploit tests (FAILS: surfaces injection/deser counterexamples)
2. Preservation baseline tests (PASS on unfixed tree)
        │  (1 and 2 are independent; both run on UNFIXED code first)
        ▼
2. PURE-VALIDATION FIXES (low risk, land first)
   3.1 #1 Snapshotter (allowlist + pathlib)
   4.1 #2 deploy.py (allowlist + shlex.quote)
   5.1 #3 run_command callers (-- sentinel + allowlist, exact argv)
   6.1 #4 test nosem
        │
        ▼
3. DESERIALIZATION FORMAT CHANGES
   7.1 #7 DIO health message (framed JSON)  ── smallest faithful change
   8.1 #5 reference map (JSON+numpy + migration shim)
        │
        ▼
4. 9.1 #6 camera frame (format change preferred; documented+enforced boundary fallback)
        │                                    ── HIGHEST preservation risk (live hot path)
        ▼
5. 10.1 #8 model_converter (weights_only=True + trusted-bucket allowlist + fallback)
        │                                    ── must not regress legit full/JIT checkpoints
        ▼
6. 11.1 #9 repo-audit gate (zero disallowed hits, minus documented nosem exceptions)
        │
        ├──────────────┐
        ▼              ▼
7. 12. Fix Checking   13. Preservation Checking
    (re-run task 1:    (re-run task 2:
     vectors neutral,   F(X) = F'(X),
     zero audit hits)   still passes)
        │              │
        └──────┬───────┘
               ▼
8. 14.1 Integration + CI-gate verification (backend suite + repo audit)
               │
               ▼
9. 15. Checkpoint (all green + CI audit guard)
```

**Critical path:** 3.1/4.1/5.1/6.1 → 7.1 → 8.1 → 9.1 → 10.1 → 11.1 → 12/13 → 14.1 → 15.

**Ordering rationale (from the design's "Ordering and risk"):** the
pure-validation fixes (#1–#4) land first because they are pure functions of their
inputs and independently testable; the deserialization changes follow in
increasing preservation risk — #7 (trivially JSON-representable), then #5 (simple
payload + migration shim), then #6 (live preview/capture hot path across a
`BaseManager` proxy, so a format change is preferred but a documented+enforced
trust boundary is the sanctioned fallback), then #8 (whose allowlisted-trusted-
source fallback is exactly what preserves legitimate full-checkpoint / JIT /
full-model loads). The repo audit (#9) is the final gate that proves no
disallowed pattern remains.

## Notes

**Bug-condition methodology reminders:**
- Task 1 is the exploration test — it is EXPECTED to surface non-empty audit hits
  and fire the deserialization sentinels on the unfixed tree (the counterexamples
  that confirm the bug). Do not "fix" it, and do not modify application source
  code in task 1.
- Task 2 captures preservation baselines that must PASS on the unfixed tree.
- Tasks 12 and 13 re-run the SAME task-1 audit/exploit tests and task-2 baselines
  against the fixed tree — every vector must be neutralized (zero disallowed audit
  hits) and the preservation tests must still pass.
- The only occurrences allowed to survive the audit are those carrying a
  documented, justified exception: the #4 test-file `# nosem` line and any
  documented+enforced trust-boundary fallback used for #6.
- Property-based testing (Hypothesis, already vendored under `.hypothesis/`) is
  emphasized wherever the input domain is generatable (metacharacter /
  option-injection strings, valid argument tuples, serialized payload structures).
- The generated CDK artifacts under `edge-cv-portal/infrastructure/cdk.out/asset.*`
  and the review's out-of-scope findings (IAM/authorization, S3 bucket-squatting,
  auth/crypto, dependency/supply-chain, Bandit B105 hardcoded strings including
  the AWS credentials in `deploy.py`) are left unchanged (Req 3.7).
```
