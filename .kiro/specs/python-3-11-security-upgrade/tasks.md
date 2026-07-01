# Implementation Plan

## Overview

This plan follows the bug-condition methodology. The interpreter pin
(`dependsOnPython(_, "3.9")`) is the bug; the fix migrates every buggy artifact
to a supported Python 3.11 while preserving all behavior that does not depend on
the 3.9 pin (notably the distro/system-python coupling for `g-ir-scanner` and the
host model-conversion scripts).

- **Property 1: Fix Checking** — for all artifacts where `isBugCondition` is true,
  the fixed artifact depends on 3.11, not 3.9 (Requirements 2.1–2.9).
- **Property 2: Preservation** — for all artifacts where `isBugCondition` is false,
  `F(X) = F'(X)` (Requirements 3.1–3.8).

## Tasks

- [x] 1. Write bug-condition exploration test (repo 3.9 audit)
  - **Property 1: Bug Condition** - End-of-life Python 3.9 pinned across the build/runtime/provisioning/doc surface
  - **CRITICAL**: This test MUST FAIL (surface non-empty hits) on the unfixed tree - the hits ARE the counterexamples that confirm the bug exists
  - **DO NOT attempt to fix the code when it surfaces hits** - this task only documents the counterexamples
  - **NOTE**: This same audit becomes the fix-checking assertion in task 11 (it must return zero hits after the fix)
  - **GOAL**: Enumerate every `dependsOnPython(_, "3.9")` artifact so the fix scope is grounded in real code
  - **Scoped PBT Approach**: The bug is deterministic, so scope the "property" to a concrete, reproducible audit over the known artifact set
  - Write an audit script/test that runs `grep -rn` for the bug-condition patterns: `python3\.9`, `3\.9` (in `-y`/`python=` args), `libpython3\.9`, `PYBIND11_PYTHON_VERSION=3\.9`, `PYTHONHOME=.*3\.9`, `get-pip\.py` `3.9` URLs, and `Python-3\.9` source tarballs
  - Scope the audit to the artifacts named in the design: `build-custom.sh`, `src/edgemlsdk/build.sh`, `src/docker-compose.yaml`, `src/backend/Dockerfile{,.jp5,.jp6}`, `src/edgemlsdk/Dockerfile{,.jp5,.jp6}`, `src/backend/edge_ml1_p_camera_management/install_edgemlsdk.sh`, `src/backend/requirements.txt`, `test/backend-test/conftest.py`, `setup-build-server.sh`, `station_install/setup_station.sh`, `station_install/patch_docker_host_prereqs.sh`, and docs (`README.md`, `README_main.md`, `test/README.md`, `TEST_COVERAGE_PLAN.md`)
  - Run the audit on the UNFIXED tree
  - **EXPECTED OUTCOME**: Audit returns NON-EMPTY hits (this is correct - it proves the 3.9 dependency exists)
  - Document the counterexamples found per artifact (e.g. `build-custom.sh: -y 3.9`; `Dockerfile.jp5: CMD ["python3.9", "app.py"]`; `Dockerfile.jp5: get-pip.py 3.9 URL`; `edgemlsdk/Dockerfile.jp5: Python3_LIBRARY=/usr/lib/libpython3.9.so` + `PYBIND11_PYTHON_VERSION=3.9`; `conftest.py: PYTHONHOME=/usr/bin/python3.9`)
  - Record the EXPECTED-to-survive (non-bug) references that the audit must NOT flag as fixes: the `g-ir-scanner` system-python shebang (`/usr/bin/python3.8` JP5 / dynamic `3.10` JP6) and the host model-conversion `python3` (system) usage - these are distro-python, not 3.9, dependencies
  - Mark task complete when the audit is written, run, and the counterexamples are documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

- [x] 2. Write preservation property tests (BEFORE implementing the fix)
  - **Property 2: Preservation** - No functional regression for non-3.9 artifacts
  - **IMPORTANT**: Follow observation-first methodology - capture the baseline on the UNFIXED (3.9) system, then assert the fixed (3.11) system matches
  - Observe and record baselines on unfixed code for the externally observable surface:
    - `import gi`, `gi.require_version('Aravis', ...)`, camera enumeration/capture succeed; record the `g-ir-scanner` shebang target (3.8 JP5 / 3.10 JP6) (Req 3.1)
    - GStreamer streaming + snapshot pipeline output (Req 3.2)
    - Triton Python-backend model load + inference results (Req 3.3)
    - FastAPI endpoint responses/status codes (auth, user/group management, API surface) (Req 3.4)
    - concurrent-camera-stream subscription/heartbeat/staleness/multi-viewer behavior (Req 3.5)
    - defect-detection workflow/trigger + DIO outputs + host model-conversion script results (Req 3.6)
    - per-target packaged artifacts for JP5/JP6/amd64 (Req 3.7)
    - existing TinyDB datastore/config read results (Req 3.8)
  - Write tests that assert the recorded baselines. Use **property-based tests** where the domain is generatable (per the design's Testing Strategy):
    - FastAPI endpoint request/response equivalence (generate varied requests; assert responses/status codes match the 3.9 baseline)
    - concurrent-camera-stream session logic (generate subscription/heartbeat/staleness event sequences; assert existing invariants hold)
    - TinyDB read/round-trip (generate records in the 3.9 layout; assert they load unchanged)
  - Add an explicit preservation assertion that the `g-ir-scanner` system-python shebang and the host model-conversion `python3` usage are UNCHANGED by the fix
  - Run the tests on the UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this captures the baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_

- [x] 3. Establish the single source of truth for the Python version
  - [x] 3.1 Parameterize `PYTHON_VERSION=3.11` across build orchestration
    - `build-custom.sh`: define `PYTHON_VERSION="${PYTHON_VERSION:-3.11}"` at the top; replace both hard-coded `-y 3.9` invocations with `-y "$PYTHON_VERSION"`; add `--build-arg PYTHON_VERSION="$PYTHON_VERSION"` to BOTH `docker-compose build` invocations (x86_64 `generic`-only branch and the `tegra`+`generic` branch)
    - `src/edgemlsdk/build.sh`: change the default `python=3.9` to `python=3.11` (`-y` already forwards to the `PYTHON_VERSION` build-arg)
    - `src/docker-compose.yaml`: add `PYTHON_VERSION` to `build.args` for each buildable service (`flask-app` and the edgemlsdk/tegra services) so the value reaches the backend Dockerfiles
    - _Bug_Condition: isBugCondition(X) where X passes/defaults `-y 3.9` / `python=3.9`_
    - _Expected_Behavior: expectedBehavior(result) — build orchestration targets 3.11 from one variable_
    - _Preservation: docker-compose service topology / profile selection unchanged_
    - _Requirements: 2.1, 2.2_

- [x] 4. Migrate the EdgeML SDK Dockerfiles to 3.11 (Triton Python backend + Panorama) — BUILD FIRST
  - [x] 4.1 `src/edgemlsdk/Dockerfile.jp5` (Ubuntu 20.04 / focal)
    - Add the deadsnakes PPA (focal) before the Python install (install `software-properties-common`, `add-apt-repository -y ppa:deadsnakes/ppa`); focal apt has no `python3.11`
    - Keep `python${PYTHON_VERSION}` / `-dev` / `-venv`; add `python${PYTHON_VERSION}-distutils`
    - Replace hard-coded `ln -s /usr/bin/python3.9 /usr/bin/python` with `/usr/bin/python${PYTHON_VERSION}`
    - Replace the four `ENV` pins to 3.11 (`PYTHON_EXECUTABLE`, `Python3_EXECUTABLE` → `/usr/bin/python3.11`; `Python3_INCLUDE_DIR` → `/usr/include/python3.11`; `Python3_LIBRARY` → the actual deadsnakes `libpython3.11.so` path, verified from the build, not assumed)
    - In the Triton build: `python3.11 build.py`, `PYTHON_EXECUTABLE=/usr/bin/python3.11`, `PYTHON_INCLUDE_DIR=/usr/include/python3.11`, `PYTHON_LIBRARY=<libpython3.11.so>` (+ `-D` variants), `PYBIND11_PYTHON_VERSION=3.11`
    - _Bug_Condition: isBugCondition(X) where X sets libpython3.9.so / PYBIND11_PYTHON_VERSION=3.9_
    - _Expected_Behavior: Triton Python backend links libpython3.11 and loads under 3.11_
    - _Requirements: 2.4, 2.5_
  - [x] 4.2 `src/edgemlsdk/Dockerfile.jp6` (Ubuntu 22.04 / jammy)
    - deadsnakes already added; change the package version (`python${PYTHON_VERSION}*`), the `ln -s .../python3.9` line, the four `ENV` pins, and the Triton `build.py` args / `PYBIND11_PYTHON_VERSION` to 3.11
    - _Bug_Condition: isBugCondition(X) where X pins 3.9 in the Triton/ENV config_
    - _Expected_Behavior: Triton Python backend links libpython3.11 and loads under 3.11_
    - _Requirements: 2.4, 2.5_
  - [x] 4.3 `src/edgemlsdk/Dockerfile` (generic, builds from source)
    - Bump the source tarball to a 3.11 release (e.g. `Python-3.11.9.tgz`), keep `--enable-optimizations --enable-shared`
    - Update `update-alternatives`, the four `ENV` pins (`/usr/local/bin/python3.11`, `/usr/local/include/python3.11`, `/usr/local/lib/libpython3.11.so`), the `ln -s /usr/bin/python3.9` line, the `python3.9 -c "import ssl..."` smoke checks, the Triton `build.py` invocation + cmake args, and the Ubuntu-18.04 `python3.6m` shim block (drive off the 3.11 include dir)
    - _Bug_Condition: isBugCondition(X) where X builds Python-3.9 from source and pins 3.9_
    - _Expected_Behavior: from-source 3.11 produces a Triton backend linking libpython3.11_
    - _Requirements: 2.4, 2.5_

- [x] 5. Bump pinned dependencies that predate 3.11
  - [x] 5.1 Update `src/backend/requirements.txt` and `install_edgemlsdk.sh` pins
    - `pyyaml==5.4.1` → `6.0.1` (5.4.1 fails under Cython ≥3.0: `AttributeError: cython_sources`)
    - `scikit-learn==1.0.2` → `>=1.1.3,<1.2` (no cp311 wheels before 1.1) — update BOTH `requirements.txt` AND the two `python3.9 -m pip install scikit-learn==1.0.2` lines in `install_edgemlsdk.sh` (which also change to `python3.11`)
    - Convert remaining `python3.9 -m pip` invocations in `install_edgemlsdk.sh` to `python3.11`; the `py3-none-any` Panorama wheel must import under 3.11
    - Verify `dlr==1.10.0` cp311 aarch64 wheel exists; if not, pin to the nearest dlr release providing one or build from source (note JP6 relies on the Neo-bundled `libdlr.so` + staged CUDA 11.4 runtime, so the pip `dlr` may only need to import)
    - Keep `numpy==1.24.3`; verify `pydantic==2.1.1` / `grpcio==1.56.2` / `grpcio-tools==1.51.1` arm64 wheels (bump only if a build fails); `pycairo==1.23.0` / `PyGObject==3.42.2` build from source against 3.11 headers
    - _Bug_Condition: isBugCondition(X) where X installs packages under python3.9 / pins versions with no cp311 wheel_
    - _Expected_Behavior: dependencies install/import under 3.11_
    - _Requirements: 2.3, 2.5_

- [x] 6. Migrate the backend (runtime) Dockerfiles to 3.11 — PRESERVE the g-ir-scanner distro-python shebang (highest risk)
  - [x] 6.1 `src/backend/Dockerfile.jp5` (Ubuntu 20.04 / focal)
    - Add the deadsnakes PPA (focal) before installing `python3.11 python3.11-dev python3.11-venv python3.11-distutils`
    - Switch `python3` to 3.11 via `update-alternatives --install/--set /usr/bin/python3 ... /usr/bin/python3.11`
    - Replace the version-specific `bootstrap.pypa.io/pip/3.9/get-pip.py` with the generic `bootstrap.pypa.io/get-pip.py` (or rely on `python3.11-distutils` + `ensurepip`)
    - Replace `python3.9 -m grpc_tools.protoc` → `python3.11 ...`, `CMD ["python3.9", "app.py"]` → `CMD ["python3.11", "app.py"]`, `python3.9 dlr_disable_phone_home.py` → `python3.11 ...`, and the `find / -name "libpython3.9.so.1.0"` diagnostic → 3.11
    - **PRESERVE** the `g-ir-scanner` shebang pointing at the SYSTEM python `/usr/bin/python3.8` — DO NOT change it (the `_giscanner` C extension is built for 3.8; this is a distro-python dependency, not a 3.9 dependency)
    - _Bug_Condition: isBugCondition(X) where X installs/selects python3.9, fetches 3.9 get-pip, runs python3.9 protoc/CMD/dlr_
    - _Expected_Behavior: image python3 and entrypoint are 3.11; gRPC stubs generated under 3.11_
    - _Preservation: g-ir-scanner system-python (3.8) shebang unchanged (Req 3.1)_
    - _Requirements: 2.3, 2.9_
  - [x] 6.2 `src/backend/Dockerfile.jp6` (Ubuntu 22.04 / jammy)
    - deadsnakes already added; change the installed package set to `python3.11*`, the `update-alternatives` target, the `get-pip.py` bootstrap, the `protoc`/`CMD`/`dlr` invocations, and the `libpython3.9` diagnostic to 3.11
    - **PRESERVE** the dynamic `g-ir-scanner` system-python detection — update its exclusion grep from `3.9` to `3.11` so it KEEPS selecting the distro 3.10 (verify it still skips the DDA version after the bump)
    - _Bug_Condition: isBugCondition(X) where X installs/selects python3.9 and runs 3.9 protoc/CMD/dlr_
    - _Expected_Behavior: image python3 and entrypoint are 3.11_
    - _Preservation: g-ir-scanner keeps selecting system 3.10 (Req 3.1)_
    - _Requirements: 2.3, 2.9_
  - [x] 6.3 `src/backend/Dockerfile` (generic, builds from source)
    - Bump the `Python-3.9.23.tgz` build to a 3.11 release; update `update-alternatives`, the `protoc`/`CMD`/`dlr` invocations, and the `libpython3.9` diagnostics to 3.11
    - _Bug_Condition: isBugCondition(X) where X builds Python-3.9 from source and runs 3.9 entrypoint_
    - _Expected_Behavior: image python3 and entrypoint are 3.11_
    - _Requirements: 2.3, 2.9_

- [x] 7. Update test configuration and the in-image test gate to 3.11
  - [x] 7.1 `test/backend-test/conftest.py` + `build-custom.sh` in-image tests
    - `conftest.py`: change `os.environ["PYTHONHOME"] = "/usr/bin/python3.9"` to the 3.11 path; prefer deriving `PYTHONHOME` from `sys.base_prefix`/`sysconfig` so it works for both deadsnakes (`/usr/bin/python3.11`) and from-source (`/usr/local/...`) layouts
    - `build-custom.sh` in-image test block: replace `python3.9 -m pip install` and `python3.9 -m pytest` with the parameterized 3.11 interpreter
    - _Bug_Condition: isBugCondition(X) where X sets PYTHONHOME=/usr/bin/python3.9 and runs python3.9 pytest/pip_
    - _Expected_Behavior: PYTHONHOME resolves to 3.11; in-image suite runs under 3.11_
    - _Requirements: 2.6_

- [x] 8. Migrate provisioning scripts to 3.11 — PRESERVE the system-python host model-conversion path
  - [x] 8.1 `setup-build-server.sh`, `station_install/setup_station.sh`, `station_install/patch_docker_host_prereqs.sh`
    - `setup-build-server.sh`: replace the "Install Python 3.9" block (PPA path + 18.04 from-source path + `update-alternatives` to `python3.9`) with 3.11
    - `station_install/setup_station.sh`: replace the `python3.9` checks/install (`install_from_source`, `install_from_ppa`), the `PYTHON39` discovery, and the symlink/`update-alternatives` lines with 3.11; **PRESERVE** the explicit "do NOT change the system `python3`" logic and the host model-conversion `python3` (system) pip installs
    - `station_install/patch_docker_host_prereqs.sh`: target 3.11 in the `for cand in /usr/local/bin/python3.9 /usr/bin/python3.9` candidate loop; keep `ensure_host_py_deps python3` (system python) intact
    - _Bug_Condition: isBugCondition(X) where X installs/selects python3.9 for DDA provisioning_
    - _Expected_Behavior: provisioning installs/selects 3.11 for DDA components_
    - _Preservation: system python3 and host model-conversion scripts unchanged (Req 3.6)_
    - _Requirements: 2.7_

- [x] 9. Update documentation to 3.11
  - [x] 9.1 `README.md`, `README_main.md`, `test/README.md`, `TEST_COVERAGE_PLAN.md`
    - Update the `python-3.9+` badges, the "Python 3.9" prose, and the `python3.9 -m pip/pytest` examples to 3.11
    - _Bug_Condition: isBugCondition(X) where a doc instructs use of python3.9_
    - _Expected_Behavior: docs describe 3.11_
    - _Requirements: 2.8_

- [x] 10. Verify the bug-condition exploration test now passes (Fix Checking)
  - **Property 1: Expected Behavior** - Supported Python 3.11 interpreter everywhere
  - **IMPORTANT**: Re-run the SAME audit from task 1 - do NOT write a new test
  - Run the repo audit from task 1 over the full artifact set
  - **EXPECTED OUTCOME**: Audit returns ZERO hits for any `3.9` reference that affects how the application is built or run (Req 2.8)
  - Confirm the surviving distro-python references are exactly the preserved ones (g-ir-scanner system-python shebang, host model-conversion `python3`) and nothing else
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9_

- [x] 11. Verify preservation tests still pass (Preservation Checking)
  - **Property 2: Preservation** - No functional regression for non-3.9 artifacts
  - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
  - Run the preservation property tests (FastAPI endpoint equivalence, concurrent-camera-stream session logic, TinyDB read round-trip) and the recorded baselines under 3.11
  - **EXPECTED OUTCOME**: Tests PASS (no regressions); the g-ir-scanner shebang and host model-conversion `python3` usage are confirmed unchanged
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_

- [x] 12. Per-target build + in-image test verification (integration)
  - [x] 12.1 Build each target with the in-image 3.11 test gate
    - Run `build-custom.sh` for JP5 (L4T r35.x / Ubuntu 20.04), JP6 (L4T r36.x / Ubuntu 22.04), and amd64; each must produce a working packaged artifact (EdgeML SDK image first, then dependency bumps, then backend image — per the design ordering)
    - In each built backend image assert: `python3 --version` == 3.11, the `CMD` interpreter is 3.11, `grpc_tools.protoc` ran under 3.11, the Triton Python backend links `libpython3.11` with `PYBIND11_PYTHON_VERSION=3.11`, the Panorama wheel imports under 3.11, and the in-image `test/backend-test` suite runs to completion under 3.11
    - _Requirements: 2.3, 2.4, 2.5, 2.6, 3.7_

- [x] 13. Runtime smoke tests for the highest-risk areas (integration)
  - [x] 13.1 Bring up the docker-compose stack on each target and smoke-test
    - `gi`/Aravis capture: `import gi`, `gi.require_version('Aravis', ...)`, enumerate + capture from a camera; confirm the g-ir-scanner shebang still points at the distro python (Req 3.1)
    - GStreamer streaming + snapshot pipeline output matches the 3.9 baseline (Req 3.2)
    - Triton inference: load a model through the rebuilt Python backend; results match the 3.9 baseline; verify the `dlr`/Neo model-load path on JP6 (CUDA 11.4 runtime staging unchanged, interpreter changed) (Req 3.3)
    - FastAPI endpoints respond with matching responses/status codes (Req 3.4)
    - concurrent-camera-stream multi-viewer streaming behaves per its existing spec (Req 3.5)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 14. Checkpoint - Ensure all tests pass
  - Confirm the task-1 audit returns zero disallowed 3.9 hits, the task-2 preservation tests pass under 3.11, all per-target builds succeed with the in-image 3.11 gate, and the runtime smoke tests pass on JP5/JP6/amd64
  - Add the repo-audit check to CI so a disallowed `3.9` reference reappearing fails the build
  - Ensure all tests pass; ask the user if questions arise

---

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1,  "description": "Run on UNFIXED code: surface 3.9 counterexamples and capture preservation baselines (independent).", "tasks": ["1", "2"] },
    { "wave": 2,  "description": "Single source of truth for PYTHON_VERSION across build orchestration.", "tasks": ["3.1"] },
    { "wave": 3,  "description": "EdgeML SDK Dockerfiles to 3.11 first (produces the Triton/Panorama artifacts the backend consumes).", "tasks": ["4.1", "4.2", "4.3"] },
    { "wave": 4,  "description": "Dependency bumps before backend pip install (pyyaml, scikit-learn, verify dlr).", "tasks": ["5.1"] },
    { "wave": 5,  "description": "Backend Dockerfiles to 3.11 (highest risk: preserve g-ir-scanner distro-python shebang).", "tasks": ["6.1", "6.2", "6.3"] },
    { "wave": 6,  "description": "Test config + in-image test gate to 3.11.", "tasks": ["7.1"] },
    { "wave": 7,  "description": "Provisioning scripts to 3.11 (preserve system-python host model-conversion path).", "tasks": ["8.1"] },
    { "wave": 8,  "description": "Documentation to 3.11.", "tasks": ["9.1"] },
    { "wave": 9,  "description": "Fix Checking and Preservation Checking (re-run tasks 1 and 2 on fixed code).", "tasks": ["10", "11"] },
    { "wave": 10, "description": "Per-target build + in-image 3.11 test gate (JP5/JP6/amd64).", "tasks": ["12.1"] },
    { "wave": 11, "description": "Runtime smoke tests for highest-risk areas (gi/Aravis, Triton, FastAPI, concurrent-stream).", "tasks": ["13.1"] },
    { "wave": 12, "description": "Checkpoint: all green + CI audit guard.", "tasks": ["14"] }
  ]
}
```

Visual summary of the critical path:

```
1. Bug-condition audit (FAILS: surfaces 3.9 counterexamples)
2. Preservation baseline tests (PASS on unfixed 3.9 tree)
        │  (1 and 2 are independent; both run on UNFIXED code first)
        ▼
3. Single source of truth (build-custom.sh / edgemlsdk/build.sh / docker-compose.yaml)
        │
        ▼
4. EdgeML SDK Dockerfiles → 3.11   ── BUILD FIRST (produces Triton/Panorama artifacts)
   4.1 jp5   4.2 jp6   4.3 generic
        │
        ▼
5. Dependency bumps (pyyaml, scikit-learn, verify dlr) ── before backend pip install
        │
        ▼
6. Backend Dockerfiles → 3.11  ── HIGHEST RISK (preserve g-ir-scanner distro-python)
   6.1 jp5   6.2 jp6   6.3 generic
        │
        ▼
7. Test config + in-image test gate → 3.11
        │
        ▼
8. Provisioning scripts → 3.11 (preserve system-python host conversion path)
        │
        ▼
9. Docs → 3.11
        │
        ├──────────────┐
        ▼              ▼
10. Fix Checking   11. Preservation Checking
    (re-run task 1:    (re-run task 2:
     ZERO 3.9 hits)     PASS under 3.11)
        │              │
        └──────┬───────┘
               ▼
12. Per-target build + in-image 3.11 test gate (JP5/JP6/amd64)
               │
               ▼
13. Runtime smoke tests (gi/Aravis, Triton, FastAPI, concurrent-stream)
               │
               ▼
14. Checkpoint (all green + CI audit guard)
```

**Critical path:** 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10/11 → 12 → 13 → 14.

**Ordering rationale (from the design's "Ordering and risk"):** the EdgeML SDK
image (task 4) is built before the backend image (task 6) because it produces the
Triton/Panorama artifacts the backend consumes; the dependency bumps (task 5)
must precede the backend build so `pip install -r requirements.txt` does not fail
on 3.11; the backend `g-ir-scanner`/distro-python preservation (task 6) is the
riskiest step and is validated by the runtime smoke tests (task 13).

## Notes

**Critical path:** 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10/11 → 12 → 13 → 14.

**Bug-condition methodology reminders:**
- Task 1 is the exploration test — it is EXPECTED to surface non-empty 3.9 hits
  on the unfixed tree (the counterexamples that confirm the bug). Do not "fix" it.
- Task 2 captures preservation baselines that must PASS on the unfixed tree.
- Tasks 10 and 11 re-run the SAME task-1 audit and task-2 tests against the fixed
  tree — the audit must return zero disallowed 3.9 hits and the preservation tests
  must still pass.
- The only 3.9-era references allowed to survive are distro-python dependencies
  (the `g-ir-scanner` system-python shebang and the host model-conversion
  `python3`), which are preserved by design.
