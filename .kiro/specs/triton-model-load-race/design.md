# Design: Atomic Triton model-repo assembly + load-queue hardening

## Goal

Guarantee Triton never observes a partially-assembled model version directory,
and ensure one model's transient load failure cannot wedge sibling model loads.

## Design overview

Two independent, complementary changes in `src/backend/dda_triton/`:

### 1. Atomic model version-directory assembly (primary fix)

`model_convertor.py` — `_create_base_model_structure`,
`_create_marshal_model_structure`, `_create_ensemble_model_structure`.

Current (racy) order for each model:
1. clean existing `<repo>/<model>` dir
2. `makedirs(<repo>/<model>/<version>)`
3. write `config.pbtxt`            ← model becomes "declared"
4. copy `model.py`                 ← RACE WINDOW (backend absent)
5. copy `inference_runtimes.py`
6. create symlinks (mochi, etc.)   ← artifacts absent until here

New (atomic) approach — **stage in a sibling temp dir, then rename into place**:

1. Build the *entire* model tree under a hidden staging path in the **same
   parent directory** (same filesystem, so `os.rename` is atomic):
   `<repo>/.staging-<model>-<pid>-<ts>/`
   - `makedirs(staging/<version>)`
   - copy `model.py`, `inference_runtimes.py` (base) / template (marshal) /
     `ensemble_model` (ensemble) into `staging/<version>`
   - create artifact symlinks into `staging/<version>`
   - write `config.pbtxt` into `staging/` **last**
2. Remove any existing `<repo>/<model>` (as today).
3. `os.replace(staging, <repo>/<model>)` — atomic directory swap.
4. On any failure, remove the staging dir and return False (never leave a
   partial dir under the real model name).

Rationale: `os.replace`/`os.rename` on the same filesystem is atomic, so a
concurrent Triton poll or `/start` sees either the old complete dir or the new
complete dir, never a half-built one. Writing `config.pbtxt` last inside the
staging dir is a second layer of safety (Triton keys off `config.pbtxt`).

A shared helper `_atomic_publish_model_dir(repo_dir, model_name, build_fn)`
encapsulates staging-dir creation, `build_fn(staging_version_dir)` invocation,
cleanup-on-error, and the atomic swap, so all three structure builders share one
correct implementation.

Filesystem note: the artifact symlinks point at absolute paths under
`/aws_dda/greengrass/.../artifacts-unarchived/...`, so moving the staging dir
does not invalidate them (absolute targets, not relative).

### 2. Load-queue hardening (resilience)

`model_convertor.py::start_model` and `triton_edge_client.py`.

- **Retry transient load failures.** `start_model` currently fires a single
  `GET /feature-configurations/models/<model>/start`. Wrap the load in a bounded
  retry-with-backoff: if the model does not reach `READY` (still `LOADING` /
  `UNAVAILABLE`) within a short budget, re-issue the load. This recovers from a
  transient missing-file/preinit failure without a full LocalServer restart.
- **Isolate per-model failures.** `TritonEdgeClient.start_triton_model` already
  loads a single model id; ensure a failure raises/*logs* for that model only
  and never propagates in a way that aborts sibling model starts. The autostart
  path iterates models independently so one failure does not short-circuit the
  rest.
- **Idempotent readiness check.** Add a small helper to poll
  `get_model_status(model_id)` up to a timeout and return a definitive
  `READY` / `NOT_READY`, used by the retry loop.

These are Python-side mitigations; the edgemlsdk C++ job-queue internals are not
modified.

## Files changed

- `src/backend/dda_triton/model_convertor.py`
  - new `_atomic_publish_model_dir(...)` helper
  - `_create_base_model_structure`, `_create_marshal_model_structure`,
    `_create_ensemble_model_structure` refactored to build-then-swap
  - `start_model(...)` bounded retry + readiness verification
- `src/backend/dda_triton/triton_edge_client.py`
  - readiness-poll helper; ensure single-model failure isolation

## Testing strategy

New tests under `test/backend-test/dda_triton/`:

- **Bug-condition (exploration) test** — assert that at no intermediate point
  during assembly does a directory named exactly `<repo>/<model>` exist while
  its `<version>/model.py` is missing (i.e., `config.pbtxt` never visible under
  the real name without `model.py`). This fails against the current
  write-in-place implementation.
- **Atomicity test** — patch `shutil.copy` / `os.symlink` to run a callback that
  inspects `<repo>/<model>` mid-build and asserts it is either absent or
  complete; confirm the final published dir contains `config.pbtxt`, `model.py`,
  and symlinks.
- **Failure cleanup test** — force a copy/symlink error mid-build and assert (a)
  no `<repo>/<model>` dir is left behind (or the prior good one is preserved),
  (b) no `.staging-*` dirs leak.
- **start_model retry test** — mock the status endpoint to return `LOADING` then
  `READY`; assert the load is retried and succeeds; mock persistent failure and
  assert bounded retries then a logged failure (no exception that would abort
  siblings).
- **Preservation tests** — existing `convert_to_triton_structure`,
  `convert_models`, and `test_triton_setup*` behavior unchanged (final published
  layout byte-identical to today; config.pbtxt content unchanged).

Run: `cd test/backend-test && python3 -m pytest dda_triton/ -q` plus the broader
backend suite for regressions.

## Backward compatibility

The final on-disk layout is identical to today (same dirs, files, config.pbtxt
content). Only the *order and atomicity* of creation change. No recipe, no
Portal, no API contract changes.
