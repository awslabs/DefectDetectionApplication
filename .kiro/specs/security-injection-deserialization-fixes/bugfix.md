# Bugfix Requirements Document

## Introduction

A code security review of the DefectDetectionApplication (DDA) — scan
`8f106a76-79c8-46a0-81ce-2775b5841b45`, "DDA Code Review" — surfaced a set of
HIGH-severity application-code findings in two related vulnerability classes:

1. **Command injection** — user-controlled or argument-controlled input is
   passed into a shell command (directly, or via a shell script / SSM
   `AWS-RunShellScript` document) without validation or quoting, enabling
   arbitrary command execution.
2. **Unsafe deserialization** — data is deserialized with `pickle`, `dill`, or
   `torch.load` (which is pickle-backed), any of which can execute arbitrary
   code embedded in a crafted payload during deserialization, enabling remote
   code execution.

This spec ("Group 1" of a larger security-review remediation effort on branch
`security-fixes-group2`) is scoped **strictly** to the application-code command
injection and unsafe-deserialization findings enumerated below. It deliberately
**excludes** the other finding classes in the same report, which are being
remediated in separate groups:

- IAM / authorization (wildcard roles, `sts:AssumeRole` scoping) — separate group.
- S3 bucket-squatting prevention (hardcoded predictable bucket names) — separate group.
- Auth / crypto findings — separate group.
- Dependency / supply-chain (e.g. `requests==2.32.3` CVE) — separate group.
- Sensitive-data-handling and Bandit B105 hardcoded-string findings, **including
  the AWS credentials embedded in `deploy.py`'s SSM command strings** — separate
  group. Only the command-injection vector in `deploy.py` is in scope here; the
  embedded-credential adjacency is noted but not fixed in this spec.
- Production-readiness disclaimers / docs — separate group.

**Important:** duplicate copies of these files that appear under
`edge-cv-portal/infrastructure/cdk.out/asset.*` are generated CDK build
artifacts that regenerate from source and are **not** in scope; only the real
source paths listed below are to be fixed.

The Group 1 findings and their real source locations:

**Command injection**

| # | File | Loc | Finding |
|---|------|-----|---------|
| 1 | `src/backend/snapshot/Snapshotter.py` | ~33 | user-controlled `stationName` concatenated into a file path passed to `subprocess.check_output(["sh", "/snapshot/snapshot.sh", path])`. |
| 2 | `src/edgemlsdk/src/test/longevity/deploy.py` | ~170 | argparse args (`platform`, `ubuntu_version`, `python_version`, `region`, `mqtt_endpoint`, `release_date`, `payload_size`, `longevity_hours`) interpolated via f-strings into shell commands sent to SSM `AWS-RunShellScript`. |
| 3 | `src/backend/utils/utils.py` | ~147 | Semgrep dangerous-subprocess-use-audit: `run_command` calls `subprocess.run` without a static string. Called by `user_group_management_utils` (useradd/userdel/gpasswd with user-supplied username/groupname) and `filesystem_management_utils` (chmod/chown/chgrp with user-supplied path/mode). |
| 4 | `test/backend-test/host_scripts/test_docker_profile_selection.py` | ~58 | Semgrep subprocess audit in a TEST file: `subprocess.run(["bash", "-c", snippet])` where `snippet` is built from static script content. Low risk. |

**Unsafe deserialization**

| # | File | Loc | Finding |
|---|------|-----|---------|
| 5 | `src/backend/lyra_science_processing_utils/model_processors/supervised_bbox_stage1_postprocessor.py` | ~53–54 | `dill.load()` (Bandit B301/B403) on `reference_image_map_file` from config. |
| 6 | `src/backend/utils/camera_manager.py` | ~39, ~593 | `import pickle` + `pickle.loads` of the frame transfer payload (B403/B301). |
| 7 | `src/backend/utils/digital_input_process_manager.py` | ~32, ~391 | `import pickle` + `pickle.loads(__shm.buf)` of the shared-memory health message (B403/B301). |
| 8 | `edge-cv-portal/backend/functions/model_converter.py` | ~100 | `torch.load(model_path, map_location='cpu')` on a user-provided S3 URI (RCE via pickle). |

The recommended remediations (per the report and located source) are:
whitelist-validate / `pathlib`-constrain `stationName` (#1); `shlex.quote()` /
allowlist-validate the deploy args (#2); validate/constrain or document the
`run_command` argument inputs while keeping arg-list (no `shell=True`) form (#3);
add a documented `nosem` justification or harden the test invocation (#4); prefer
a safe format (JSON) or validate/document the trust boundary for the `dill` /
`pickle` loads (#5, #6, #7); and `torch.load(..., weights_only=True)` and/or
restrict to trusted buckets/accounts (#8).

This requirement also drives a **repo audit** (grep for `subprocess` with shell
string interpolation, and for `pickle` / `dill` / `torch.load`) as the
exploration test: it must surface these counterexamples on the unfixed tree and
return zero disallowed hits (outside documented, justified exceptions) after the
fix.

### Bug Condition and Properties

The bug-condition methodology frames this fix as follows.

**Bug Condition `C(X)`** — identifies the inputs/code paths that trigger the
defect. Here the "input" is any application-code path that carries data into a
shell command or an unsafe deserializer:

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type CodePath   // an application code path that carries an input
                              // value into a shell command or a deserializer
  OUTPUT: boolean

  // True when an untrusted / user-controlled (or argument-controlled) input is
  // passed into a shell command OR into an unsafe deserializer
  // (pickle / dill / torch.load) in a way that can enable code or command
  // execution.
  RETURN reachesShellCommand(X, untrustedInput(X))        // command injection
      OR reachesUnsafeDeserializer(X, untrustedInput(X))  // pickle/dill/torch.load
END FUNCTION
```

**Fix Property `P` (Fix Checking)** — desired behavior for all buggy inputs
after the fix `F'`:

```pascal
// Property: Fix Checking - the injection / deserialization vector is neutralized
FOR ALL X WHERE isBugCondition(X) DO
  result ← F'(X)
  ASSERT neutralized(result)
     // command injection: shell metacharacters / option-injection payloads are
     //   REJECTED (validation error) or rendered INERT (quoted / passed as a
     //   non-shell argument) — they never reach a shell as executable syntax.
     // deserialization: a crafted malicious payload CANNOT trigger arbitrary
     //   code execution (safe format, weights_only, or a documented + enforced
     //   trust boundary that the payload cannot cross).
END FOR
```

**Preservation Property (Preservation Checking)** — for every NON-malicious,
legitimate input, the fixed code behaves identically to the original code `F`:

```pascal
// Property: Preservation Checking - no behavior change for legitimate inputs
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
     // valid stationNames still snapshot; valid deploy args still produce the
     // same SSM commands; valid usernames/groupnames/paths/modes still run the
     // same privileged commands; legitimate serialized data (reference map,
     // camera frame, health message) still round-trips; valid model files still
     // load and convert.
END FOR
```

- **F**: the original (unfixed) code, where the untrusted input reaches the
  shell / deserializer directly.
- **F'**: the fixed code, where the vector is validated, quoted, replaced with a
  safe format, or constrained to a documented trust boundary.

Where the input domain is generatable (strings, argument values, serialized
payloads), **property-based testing** is emphasized: generate strings containing
shell metacharacters / option-injection payloads and assert they are rejected or
quoted (Fix Checking), while generating valid inputs and asserting identical
behavior (Preservation); for deserialization, assert a crafted malicious payload
cannot trigger code execution while valid payloads deserialize correctly.

## Bug Analysis

### Current Behavior (Defect)

The application passes untrusted or argument-controlled input into shell commands
and unsafe deserializers without validation, quoting, or a safe format.

1.1 WHEN `take_snapshot(stationName)` in `src/backend/snapshot/Snapshotter.py`
receives a `stationName` containing shell metacharacters (e.g.
`"test; rm -rf /"`) THEN the system concatenates it into
`path = "/aws_dda/system/snapshot-<stationName>-<timestamp>.tar"` and passes it
to `subprocess.check_output(["sh", "/snapshot/snapshot.sh", path])`, where the
shell script can interpret the metacharacters, enabling arbitrary command
execution.

1.2 WHEN `src/edgemlsdk/src/test/longevity/deploy.py` is invoked with argparse
arguments (`platform`, `ubuntu_version`, `python_version`, `region`,
`mqtt_endpoint`, `release_date`, `payload_size`, `longevity_hours`) that contain
shell metacharacters (e.g. `--platform 'aarch64; rm -rf /'`) THEN the system
interpolates them via f-strings into shell command strings that are sent to SSM
with `DocumentName="AWS-RunShellScript"` and executed as shell commands on the
EC2 instance, enabling arbitrary command execution.

1.3 WHEN `run_command(command)` in `src/backend/utils/utils.py` is invoked
(through `user_group_management_utils` with a user-supplied `username`/`groupname`,
or through `filesystem_management_utils` with a user-supplied `path`/`mode`) THEN
the system passes the value to `subprocess.run(command, capture_output=True)`
without validating the input; a Semgrep dangerous-subprocess-use-audit flags the
non-static `subprocess.run`, and an argument-controlled value (e.g. a
username/path beginning with `-`) can be interpreted as an option by the invoked
tool (`useradd`, `userdel`, `gpasswd`, `chmod`, `chown`, `chgrp`).

1.4 WHEN the tests in
`test/backend-test/host_scripts/test_docker_profile_selection.py` run THEN the
system builds a bash snippet from the script's own content and executes
`subprocess.run(["bash", "-c", snippet])`, which a Semgrep subprocess audit flags
as dynamic subprocess use even though the snippet is derived from static,
in-repo script content.

1.5 WHEN `SupervisedBBoxStage1PostProcessor` in
`src/backend/lyra_science_processing_utils/model_processors/supervised_bbox_stage1_postprocessor.py`
is constructed with `config1['reference_image_map_file']` THEN the system opens
the file and calls `data = dill.load(handle)`, and a crafted file can execute
arbitrary code during deserialization.

1.6 WHEN `src/backend/utils/camera_manager.py` transfers a camera frame — the
`Camera.get_frame()` path returns `pickle.dumps(...)` and
`_get_camera_frame`/`get_camera_frame` call `pickle.loads(camera.get_frame())` —
THEN the system deserializes the frame payload with `pickle`, and untrusted or
corrupted pickle bytes would execute arbitrary code during deserialization
(`import pickle` at ~line 39, `pickle.loads` at ~line 593).

1.7 WHEN `get_dio_process_health_report(workflow_id)` in
`src/backend/utils/digital_input_process_manager.py` reads the shared-memory
health buffer and calls `pickle.loads(__shm.buf)` THEN the system deserializes
the buffer contents with `pickle`, and attacker-influenced buffer contents would
execute arbitrary code during deserialization (`import pickle` at ~line 32,
`pickle.loads` at ~line 391).

1.8 WHEN `convert_model` / `inspect_pytorch_model` in
`edge-cv-portal/backend/functions/model_converter.py` receives a user-provided
`model_s3_uri`, downloads the referenced file, and calls
`torch.load(model_path, map_location='cpu')` without `weights_only=True` THEN a
malicious `.pt` file (pickle-backed) executes arbitrary code (remote code
execution) inside the Lambda during load.

### Expected Behavior (Correct)

The application validates, quotes, or safely handles every input before it
reaches a shell command or a deserializer, so that malicious inputs are rejected
or rendered inert.

2.1 WHEN `take_snapshot(stationName)` receives a `stationName` containing shell
metacharacters THEN the system SHALL reject it (e.g. by validating against
`^[a-zA-Z0-9_-]+$` and raising a client error such as HTTP 400) and/or constrain
the constructed path with `pathlib` so it stays within `/aws_dda/system/`, so no
metacharacter can reach the shell script as executable syntax.

2.2 WHEN `deploy.py` is invoked with argparse arguments containing shell
metacharacters THEN the system SHALL neutralize them before they reach SSM —
`shlex.quote()` each interpolated argument and/or validate each against a strict
allowlist (rejecting shell metacharacters such as `; | & \` $ ( )`) — so the
constructed `AWS-RunShellScript` commands cannot execute injected commands.

2.3 WHEN `run_command` is invoked through `user_group_management_utils` /
`filesystem_management_utils` with an argument-controlled `username` / `groupname`
/ `path` / `mode` THEN the system SHALL constrain those inputs (validate against
an allowlist and reject option-injection such as a leading `-`, and/or use an
end-of-options `--` sentinel where the tool supports it) OR document the trust
boundary if the inputs are already validated upstream; in all cases the code
SHALL continue to avoid `shell=True` and pass arguments as a list.

2.4 WHEN the `test_docker_profile_selection.py` tests run THEN the system SHALL
either add a documented `# nosem` justification (the snippet is derived from
static, in-repo script content, test-only) or harden the invocation, so the
audit finding is resolved with a recorded rationale; this finding is treated as
low-risk.

2.5 WHEN `SupervisedBBoxStage1PostProcessor` loads
`config1['reference_image_map_file']` THEN the system SHALL prefer a safe
serialization format (e.g. JSON) for the reference-image map, OR validate and
explicitly document the trust boundary (the file is application-generated and
integrity-verified) and constrain the load, so that a crafted file cannot
execute arbitrary code.

2.6 WHEN `camera_manager.py` transfers a camera frame THEN the system SHALL
replace `pickle` with a safe serialization for the frame payload, OR explicitly
document and enforce the trust boundary (the producer is an in-process, trusted
`BaseManager`-hosted `Camera`), so that no untrusted data is deserialized with
`pickle`.

2.7 WHEN `digital_input_process_manager.py` reads the shared-memory health
message THEN the system SHALL apply the same treatment as 2.6 (safe serialization
for the health message, or a documented + enforced trust boundary for the
process-local shared-memory buffer), so that no untrusted data is deserialized
with `pickle`.

2.8 WHEN `convert_model` / `inspect_pytorch_model` loads a user-provided model
file THEN the system SHALL call `torch.load(model_path, map_location='cpu',
weights_only=True)` and/or restrict the `model_s3_uri` to trusted
buckets/accounts, so that a malicious `.pt` file cannot execute arbitrary code
during load.

2.9 WHEN the repository is audited for the bug-condition patterns (`subprocess`
with shell string interpolation, and `pickle` / `dill` / `torch.load` on
externally-influenced data) THEN the system SHALL contain no remaining disallowed
occurrence in the in-scope application code, other than occurrences carrying a
documented, justified exception (e.g. a `nosem`/comment recording an enforced
trust boundary).

### Unchanged Behavior (Regression Prevention)

All legitimate, non-malicious behavior must continue to work exactly as before.
For every input that does NOT trigger the bug condition, the fixed system must
behave identically to the original.

3.1 WHEN `take_snapshot` receives a valid `stationName` (matching
`^[a-zA-Z0-9_-]+$`) THEN the system SHALL CONTINUE TO construct the same
`snapshot-<stationName>-<timestamp>.tar` path, invoke the snapshot script, and
return `"snapshotfile/<file>.gz"` exactly as before.

3.2 WHEN `deploy.py` is invoked with legitimate arguments (valid platform,
ubuntu/python versions, region, MQTT endpoint, release date, numeric sizes) THEN
the system SHALL CONTINUE TO construct equivalent SSM commands and deploy / run
the longevity tests unchanged.

3.3 WHEN `run_command` and the user-group / filesystem utilities are called with
valid usernames, groupnames, paths, and modes THEN the system SHALL CONTINUE TO
create/delete users and groups and apply `chmod`/`chown`/`chgrp` exactly as
before, returning the same `(success, output)` contract.

3.4 WHEN the docker-profile-selection tests run THEN the system SHALL CONTINUE TO
exercise the real script decision block and pass (the `tegra`/`generic` selection
assertions and the regression guards remain unchanged).

3.5 WHEN a legitimate, application-generated `reference_image_map_file`, camera
frame payload, or shared-memory health message is loaded THEN the system SHALL
CONTINUE TO deserialize it into the same data structures, and the postprocessor,
camera preview/capture path, and health-report path SHALL behave identically to
before.

3.6 WHEN a legitimate PyTorch model file (raw state dict, checkpoint, or JIT
model) is provided to the model converter THEN the system SHALL CONTINUE TO
inspect it and produce the same detected metadata and generated DDA package; the
hardened load path SHALL still successfully load the intended tensors for
legitimate models.

3.7 WHEN the review's out-of-scope findings are considered — IAM/authorization,
S3 bucket-squatting, auth/crypto, dependency/supply-chain, sensitive-data-handling
and Bandit B105 hardcoded strings (including the AWS credentials embedded in
`deploy.py`'s SSM commands), and production-readiness/docs — THEN this spec SHALL
CONTINUE TO leave them unchanged, and SHALL NOT modify the generated CDK build
artifacts under `edge-cv-portal/infrastructure/cdk.out/asset.*`, as those are
handled by separate remediation groups or regenerate from source.
