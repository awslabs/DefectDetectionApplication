# Implementation Plan

- [x] 1. Write bug-condition exploration test for non-atomic assembly
  - Add `test/backend-test/dda_triton/test_model_convertor_atomicity_bug.py`.
  - Drive `convert_to_triton_structure` against a fixture model tree; wrap
    `shutil.copy`/`os.symlink` so a probe runs during assembly and records
    whether `<repo>/<model>` (real name) ever exists while its
    `<version>/model.py` is missing.
  - Assert the invariant "never visible-but-incomplete"; expected to FAIL on the
    current in-place implementation (confirms the race).
  - _Requirements: root cause #1_

- [x] 2. Implement atomic staging-and-swap helper
  - Add `_atomic_publish_model_dir(model_repo_dir, model_name, build_fn)` to
    `src/backend/dda_triton/model_convertor.py`: create
    `<repo>/.staging-<model>-*/` (tempfile.mkdtemp, same-fs sibling), invoke
    `build_fn(staging_dir)`, write nothing under the real name until a final
    `os.replace(staging, <repo>/<model>)`; remove prior real dir just before the
    swap; on exception remove the staging dir and return False.
  - _Requirements: root cause #1_

- [x] 3. Refactor the three structure builders to build-then-swap
  - Rework `_create_base_model_structure`, `_create_marshal_model_structure`,
    `_create_ensemble_model_structure` to assemble into the staging dir via the
    helper, copying `model.py`/`inference_runtimes.py`/template/`ensemble_model`
    and creating symlinks first, and writing `config.pbtxt` last inside staging.
  - Preserve the exact final layout and `config.pbtxt` content.
  - _Requirements: root cause #1_

- [x] 4. Verify exploration test now passes + add atomicity/cleanup tests
  - Confirmed task 1's invariant test passes post-refactor.
  - Added `test_model_convertor_atomicity_fix.py`: atomicity test (mid-build
    probe sees absent-or-complete for base/marshal/ensemble), failure-cleanup
    test (forced mid-build error leaves no partial `<model>` dir and no leaked
    `.staging-*`), and prior-dir-preserved-on-failure test.
  - _Requirements: root cause #1_

- [x] 5. Harden start_model with bounded retry + readiness verification
  - `model_convertor.py::start_model` now polls model status after `/start`;
    retries with backoff if not `READY` within a budget; bounded attempts then a
    logged error (never raises, so a sibling model start is unaffected). Added
    `_get_triton_model_state` and `_wait_for_model_ready` helpers. 403 (already
    loading) is treated as in-flight and waited out.
  - _Requirements: root cause #2_

- [x] 6. Add start_model retry/isolation tests
  - Added `test_start_model_retry.py`: LOADING→READY success, already-READY
    short-circuit, bounded-retry-then-give-up (no raise), terminal-state
    short-circuit, 403-as-in-flight, connection-error containment, and
    sibling-isolation.
  - _Requirements: root cause #2_

- [x] 7. Preservation + regression run
  - Ran the dda_triton suite in the flask-app image (native deps + LD paths as
    the build does). All new tests green; convert/clean/autostart tests pass.
    The only 2 failures (`test_lfv_to_triton` stop/conversion-failure) are
    PRE-EXISTING — they fail identically with the original `model_convertor.py`
    stashed, and exercise `switch_to_triton` (unrelated to this fix, with
    `convert_models` fully mocked).
  - _Requirements: backward compatibility_
