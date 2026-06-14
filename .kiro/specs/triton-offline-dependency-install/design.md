# Triton Offline Dependency Install Bugfix Design

## Overview

On the `python_310` branch the DDA backend container installs its model conversion
dependencies at **runtime**. During startup, `app.setup_triton()` calls
`dda_triton.triton_setup.create_virtual_env()`, which shells out to
`python3 -m pip install -r /dda_triton/model_conversion_requirements.txt`. This pip
call reaches out to a remote package index. On offline / air-gapped edge devices
(e.g. the Jetson Xavier NX `amazoncam-xavier-nx`) there is no internet or DNS, so the
install fails with name-resolution errors and `No matching distribution found for
protobuf==4.25.8`. The pinned model conversion dependencies are therefore never
installed and the Triton/model setup is left broken.

The fix moves the model conversion dependency install from runtime to **build time**.
The pinned dependencies in `src/backend/dda_triton/model_conversion_requirements.txt`
(setuptools, wheel, meson, grpcio==1.56.2, grpcio-tools==1.51.1, protobuf==4.25.8,
requests==2.32.3, opencv-python, urllib3==2.2.3, scikit-learn==1.0.2, numpy==1.24.3)
are baked into all three backend images (`Dockerfile`, `Dockerfile.jp5`,
`Dockerfile.jp6`) so they are already importable when the container starts. The
runtime `create_virtual_env()` is changed so it no longer performs a
network-dependent install; instead it verifies the dependencies are already present
and logs accordingly, succeeding without network access.

The general strategy is: install at build time, verify (don't install) at runtime,
and preserve every other behavior — the online path, the `cp_model_conversion_files`
copy step, the existing `requirements.txt` install, and all three build variants.

## Glossary

- **Bug_Condition (C)**: The backend container performs its Triton/model setup
  (`setup_triton` → `create_virtual_env`) and the model conversion dependencies are
  not already present in the image, forcing a runtime `pip install` that requires
  network access — which fails on an offline device.
- **Property (P)**: The Triton/model setup completes successfully with all model
  conversion dependencies present and importable, without performing any
  network-dependent install at runtime.
- **Preservation**: Behavior that must remain unchanged — the online execution path,
  the `cp_model_conversion_files` copy of model conversion files and resources, the
  existing `requirements.txt` install, the pinned dependency versions, and a working
  backend image across the standard, jp5, and jp6 build variants.
- **create_virtual_env**: The function in
  `src/backend/dda_triton/triton_setup.py` that today runs
  `python3 -m pip install -r /dda_triton/model_conversion_requirements.txt` at
  container startup. Invoked by `setup_triton()` in `src/backend/app.py`.
- **cp_model_conversion_files**: The function in
  `src/backend/dda_triton/triton_setup.py` that copies model conversion files
  (`constants.py`, `model_config_pb2.py`, `model_autostart_utils.py`,
  `model_convertor.py`, `convert_model_cleanup.py`,
  `model_conversion_requirements.txt`) and resources to their destination folders.
- **setup_triton**: The function in `src/backend/app.py` that calls
  `create_virtual_env()` then `cp_model_conversion_files()` during startup.
- **model_conversion_requirements.txt**: The pinned model conversion dependency list
  at `src/backend/dda_triton/model_conversion_requirements.txt`, copied into the image
  under `dda_triton/` and referenced at runtime as
  `/dda_triton/model_conversion_requirements.txt`.

## Bug Details

### Bug Condition

The bug manifests when the backend container runs its Triton/model setup at startup
on a device with no internet or DNS. `create_virtual_env()` finds the requirements
file and runs a live `pip install` against a remote package index. Because the model
conversion dependencies were never baked into the image, the runtime install is the
only mechanism that would make them available — and it cannot reach the index, so it
fails and the dependencies remain absent.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type TritonSetupInvocation
    - input.networkAvailable: boolean   (can the device reach a package index)
    - input.depsBakedIntoImage: boolean (were model conversion deps installed at build time)
    - input.runtimePipInstall: boolean  (does create_virtual_env attempt a pip install)
  OUTPUT: boolean

  RETURN input.runtimePipInstall == true
         AND input.depsBakedIntoImage == false
         AND input.networkAvailable == false
END FUNCTION
```

In words: the bug condition holds whenever the runtime setup depends on a live pip
install to obtain the model conversion dependencies AND those dependencies are not
already in the image AND the device is offline.

### Examples

- **Offline Xavier NX (primary defect)**: Container starts on `amazoncam-xavier-nx`
  with no DNS. `create_virtual_env()` runs
  `python3 -m pip install -r /dda_triton/model_conversion_requirements.txt`. Expected:
  setup completes with all deps present. Actual: `[Errno -3] Temporary failure in name
  resolution`, install aborts, deps absent.
- **Offline device, pinned package**: pip cannot reach the index and reports `Could
  not find a version that satisfies the requirement protobuf==4.25.8` / `No matching
  distribution found for protobuf==4.25.8`; the install returns a non-zero exit status.
- **Post-failure model conversion**: Because the install failed, `protobuf`,
  `grpcio-tools`, `scikit-learn`, etc. are missing, so model conversion / Triton setup
  cannot proceed correctly. Expected: all dependencies importable so conversion runs.
- **Edge case — online device today**: pip reaches the index and installs the deps, so
  setup happens to succeed. This is NOT the bug condition, and the fix must keep it
  working.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- On a device that DOES have network access, all model conversion dependencies remain
  available and the Triton/model setup continues to complete successfully (Req 3.1).
- `cp_model_conversion_files()` continues to copy the model conversion files and
  resources to their destination folders exactly as today (Req 3.2).
- The build continues to install the existing backend `requirements.txt` dependencies,
  and all other build steps continue to produce a working backend image across the
  standard, jp5, and jp6 variants (Req 3.3).
- Model conversion continues to use the same pinned dependency versions, preserving
  existing conversion and inference behavior (Req 3.4).

**Scope:**
All behavior that does NOT involve obtaining the model conversion dependencies at
runtime should be completely unaffected by this fix. This includes:
- The online execution path (network-available devices).
- The file/resource copy step (`cp_model_conversion_files`).
- The existing `requirements.txt` install and all other Docker build steps.
- The pinned dependency versions used during model conversion.

**Note:** The actual expected correct behavior (offline setup succeeds with deps
present) is defined in the Correctness Properties section (Property 1). This section
focuses on what must NOT change.

## Hypothesized Root Cause

Based on the bug analysis, the root cause is well understood:

1. **Runtime install of model conversion deps (primary cause)**: The model conversion
   dependencies are obtained by a runtime `pip install -r
   /dda_triton/model_conversion_requirements.txt` inside `create_virtual_env()` rather
   than being baked into the image. On an offline device this install has no package
   index to resolve against and fails.

2. **No build-time install of model conversion deps**: None of the three Dockerfiles
   (`Dockerfile`, `Dockerfile.jp5`, `Dockerfile.jp6`) install
   `model_conversion_requirements.txt`. They install the backend `requirements.txt`
   (and other pip packages), but the model conversion pins are deferred to runtime.

3. **Runtime step is network-coupled**: `create_virtual_env()` unconditionally runs
   the install whenever the requirements file exists, coupling a startup-critical setup
   path to network availability.

The fix targets causes 1–3 directly: install at build time (cause 2), and change the
runtime step to verify rather than install (causes 1 and 3).

## Correctness Properties

Property 1: Bug Condition - Offline Triton Setup Succeeds With Baked-In Dependencies

_For any_ Triton/model setup invocation where the bug condition holds (offline device
relying on a runtime install for dependencies not baked into the image), the fixed code
SHALL complete setup successfully with all pinned model conversion dependencies
(setuptools, wheel, meson, grpcio==1.56.2, grpcio-tools==1.51.1, protobuf==4.25.8,
requests==2.32.3, opencv-python, urllib3==2.2.3, scikit-learn==1.0.2, numpy==1.24.3)
already present and importable, without performing any network-dependent install at
runtime.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation - Online Path, File Copy, Build Steps, and Pinned Versions Unchanged

_For any_ input where the bug condition does NOT hold (network-available devices, the
file/resource copy step, the existing `requirements.txt` install, other build steps,
and model conversion version selection), the fixed code SHALL produce the same
observable result as the original code, preserving the online setup success, the
`cp_model_conversion_files` behavior, a working image across the standard/jp5/jp6
variants, and the pinned dependency versions used for conversion.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

## Fix Implementation

### Changes Required

Assuming the root cause analysis is correct, the fix has two parts: bake the
dependencies in at build time, and make the runtime step verify rather than install.

**File**: `src/backend/dda_triton/model_conversion_requirements.txt`

No content change required — this remains the single source of truth for the pinned
versions, used by both the build-time install and (for verification) at runtime. Its
copy by `cp_model_conversion_files()` is preserved.

**File**: `src/backend/Dockerfile` (standard variant)

1. **Make the requirements file available early**: `COPY
   dda_triton/model_conversion_requirements.txt ./model_conversion_requirements.txt`
   (alongside the existing `COPY requirements.txt ./`), so it can be installed before
   the late `COPY dda_triton ./dda_triton`.
2. **Add a build-time install step**: `RUN python3.11 -m pip install --no-cache-dir -r
   ./model_conversion_requirements.txt`, placed after the existing backend
   `requirements.txt` install path (`prereqs_install.sh`) so the pinned model
   conversion deps are baked into the image's Python environment.

**File**: `src/backend/Dockerfile.jp5`

1. **Make the requirements file available**: `COPY
   dda_triton/model_conversion_requirements.txt
   ./model_conversion_requirements.txt`.
2. **Add a build-time install step** after the existing `RUN pip install
   --no-cache-dir -r ./requirements.txt`: `RUN pip install --no-cache-dir -r
   ./model_conversion_requirements.txt`. Order it so it does not undo the deliberate
   `setuptools<81` pin needed by `grpc_tools.protoc` (install model conversion deps and
   re-assert the setuptools cap if necessary, since the requirements list includes an
   unpinned `setuptools`).

**File**: `src/backend/Dockerfile.jp6`

1. **Make the requirements file available**: `COPY
   dda_triton/model_conversion_requirements.txt
   ./model_conversion_requirements.txt`.
2. **Add a build-time install step** after the existing `RUN pip install
   --no-cache-dir -r ./requirements.txt`, with the same `setuptools<81` ordering
   consideration as jp5.

**File**: `src/backend/dda_triton/triton_setup.py`

3. **Convert `create_virtual_env` from install to verify**: Replace the
   `subprocess.check_call(... pip install ...)` block with a verification step that
   confirms the model conversion dependencies are already importable. Concretely:
   - Do NOT run any `pip install` (no network access at runtime).
   - Parse the package names from `model_conversion_requirements.txt` (when present)
     and attempt to import / locate each distribution (e.g. via
     `importlib.metadata.version` / `importlib.util.find_spec`).
   - Log success when all are present; log a clear, actionable error listing any
     missing packages (this would indicate a build-time regression, not a runtime
     network problem).
   - Keep the function signature and defaults (`python_path`, `requirements_file`) so
     `setup_triton()` and existing callers are unaffected.
   - Continue to handle a missing requirements file gracefully (log and skip), matching
     today's behavior.

4. **Leave `cp_model_conversion_files` unchanged**: The copy of model conversion files
   and resources is preserved exactly.

**File**: `src/backend/app.py`

5. **No change to `setup_triton` call order**: It continues to call
   `create_virtual_env()` then `cp_model_conversion_files()`. Only the internal
   behavior of `create_virtual_env()` changes (verify instead of install).

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that
demonstrate the bug on the unfixed code (runtime install failing without network),
then verify the fix works correctly (deps baked in, runtime verifies offline) and
preserves existing behavior (online path, file copy, build steps, pinned versions).

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix.
Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Exercise `create_virtual_env()` (and the surrounding `setup_triton`
flow) in conditions that simulate an offline device with the dependencies absent, and
observe that the runtime pip install is attempted and fails. Run these tests on the
UNFIXED code to observe failures and confirm the network coupling is the cause.

**Test Cases**:
1. **Offline runtime install fails**: Simulate `create_virtual_env()` with no network
   (mock `subprocess.check_call` to raise a `CalledProcessError`/name-resolution error,
   or run with the index unreachable). Assert the install is attempted and the failure
   surfaces (will fail / error on unfixed code).
2. **Dependencies absent after failed install**: After the simulated failed install,
   assert that the pinned packages (e.g. `protobuf==4.25.8`, `grpcio-tools`,
   `scikit-learn`) are not importable (demonstrates the broken setup state on unfixed
   code).
3. **Runtime install is network-coupled**: Assert that on unfixed code
   `create_virtual_env()` issues a `pip install` command (the network-dependent
   behavior) whenever the requirements file is present (confirms root cause).
4. **Edge case — missing requirements file**: Invoke with a non-existent
   `requirements_file` and confirm the existing graceful skip/log behavior (baseline to
   preserve).

**Expected Counterexamples**:
- `create_virtual_env()` attempts `python3 -m pip install -r
  /dda_triton/model_conversion_requirements.txt` and fails offline with name-resolution
  / `No matching distribution found for protobuf==4.25.8`.
- Possible causes: runtime install of deps not baked into image, no build-time install,
  network-coupled startup step — all confirmed by the cases above.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed code
produces the expected behavior (offline setup succeeds with deps present, no runtime
network install).

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := create_virtual_env_fixed(input)   // deps baked into image, no network
  ASSERT result performs NO pip install / no network call
  ASSERT all model conversion deps are importable
  ASSERT setup_triton completes successfully
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed
code produces the same result as the original code.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT behavior_original(input) = behavior_fixed(input)
END FOR
```

Covers: online devices (setup still succeeds), `cp_model_conversion_files` (same files
and resources copied to the same destinations), the existing `requirements.txt` install
and other build steps (image still builds and runs across standard/jp5/jp6), and the
pinned dependency versions used for conversion (unchanged).

**Testing Approach**: Property-based testing is recommended for preservation checking
because:
- It generates many test cases automatically across the input domain (e.g. varied
  present/absent dependency sets, varied destination folder states for the copy step).
- It catches edge cases that manual unit tests might miss.
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs.

**Test Plan**: Observe behavior on UNFIXED code first for the online path, the file
copy, and the build, then write tests capturing that behavior and assert it is
preserved after the fix.

**Test Cases**:
1. **Online path preservation**: Observe on unfixed code that, with network and deps
   available, setup completes; assert the fixed code also completes setup with all deps
   importable.
2. **File copy preservation**: Observe on unfixed code which files
   (`constants.py`, `model_config_pb2.py`, `model_autostart_utils.py`,
   `model_convertor.py`, `convert_model_cleanup.py`,
   `model_conversion_requirements.txt`) and resources are copied where; assert the fixed
   `cp_model_conversion_files()` copies the same set to the same destinations.
3. **Pinned version preservation**: Assert the baked-in versions match the pins in
   `model_conversion_requirements.txt` exactly (grpcio==1.56.2, protobuf==4.25.8,
   urllib3==2.2.3, scikit-learn==1.0.2, numpy==1.24.3, etc.).

### Unit Tests

- `create_virtual_env()` verification logic: all deps present → success log, no pip
  install issued; some deps missing → clear error listing missing packages, still no
  pip install; missing requirements file → graceful skip (unchanged).
- `cp_model_conversion_files()` copies the expected files and resources (regression
  guard).
- `setup_triton()` calls `create_virtual_env()` then `cp_model_conversion_files()` and
  swallows/logs exceptions as today.

### Property-Based Tests

- Generate random subsets of "present" vs "missing" model conversion dependencies and
  verify the fixed `create_virtual_env()` never performs a network install and reports
  presence/absence correctly.
- Generate random destination-folder pre-states and verify `cp_model_conversion_files()`
  copies the same files to the same destinations as the original (preservation).
- Verify across many simulated invocations that no runtime `pip install` / network call
  is ever issued by the fixed setup path.

### Integration Tests

- Build each image variant (`Dockerfile`, `Dockerfile.jp5`, `Dockerfile.jp6`) and
  assert the pinned model conversion dependencies are importable in the built image
  (build-time bake-in works for all three variants).
- Run the backend container with network disabled and assert `setup_triton` completes,
  `create_virtual_env` issues no install, and model conversion dependencies are
  importable (full offline flow).
- Run the backend container with network enabled and assert setup still completes and
  the file/resource copy still occurs (online preservation).
