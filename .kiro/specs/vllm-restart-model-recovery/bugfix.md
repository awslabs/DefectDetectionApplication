# vLLM Restart Model Recovery

## Summary

After a raw restart of the LocalServer backend container (e.g. `docker
restart`, not a Greengrass deployment), the deployed vLLM model is
unusable: it reports "loading" (STAGED) forever, and once a load is
finally driven, the first attempt fails with a KV-cache out-of-memory
that then repeats on every plain retry. Observed and recovered manually
several times on ryan-orin-nano (JetPack 6, LocalServer v1.0.56) with
model `model-vllm-qwen-2-5-7b`.

## Observed defects

1. **Stuck STAGED after container restart.** Model loads are only issued
   by the Greengrass component lifecycle (`vllm_model_prep.py` Startup
   -> `POST 127.0.0.1:8901/v2/repository/models/{m}/load`). The runtime
   (`vllm_runtime`) scans the staged repositories and reports STAGED,
   but nothing ever drives a load after a bare container restart, so the
   UI shows "loading" indefinitely.
2. **First load post-restart OOMs, and stays OOMing.** The first load
   regularly fails with `No available memory for the cache blocks. Try
   increasing gpu_memory_utilization...`. The failed engine
   construction leaves ~14GB of GPU allocations pinned in the runtime
   process (no engine object exists to shut down, and
   `manager._fail` had nothing to release), so every subsequent plain
   load retry OOMs identically.
3. **Validated manual recovery:** `POST .../unload` then
   `POST .../load` — the unload releases the pinned memory and the
   immediately following load succeeds. Used repeatedly on device.
4. **Runtime server blocks its event loop during load** — status
   endpoints stall while an engine initializes (minutes for a 7B
   model).

## Fixes implemented

### A. Encode the unload -> reload recovery in the load driver

`src/backend/dda_triton/vllm_model_prep.py` `request_load`: when the
authoritative HTTP load failure matches the KV-cache markers
(`KV_CACHE_HINT_MARKERS`), run exactly one recovery cycle — unload, then
retry the load once. A genuinely oversized model fails the retry too and
exits with the existing sizing remediation hint. Every other HTTP error
keeps the original single-attempt semantics (preserved by
`test/backend-test/deploy_reliability/test_vllm_prep_error_paths_preservation.py`,
whose goldens use non-KV bodies and pass unchanged).

Intentional golden update:
`test/backend-test/dda_triton/test_vllm_load_failure_log.py` now asserts
the load/unload/load cycle for the KV-cache body (both outcomes:
recovery success -> LOAD_OK, retry failure -> LOAD_HTTP_ERROR) and the
unchanged single-attempt behavior for non-KV errors.

### B. Release GPU memory on failed load / unload in the runtime

`src/backend/vllm_runtime/manager.py`: `_reclaim_gpu_memory` (gc +
lazy-imported `torch.cuda.empty_cache()`, strictly best-effort) runs
after every failure transition (`_fail` — covering the
engine-construction OOM where no engine object exists) and after every
`unload`. With the allocator returned to a clean state, the component's
existing restart-retry loop can succeed without the unload dance.

Tests: `test/backend-test/vllm_runtime/test_manager_memory_reclaim.py`.

## Follow-up (not implemented here)

- **Auto-load STAGED models at runtime startup** (fixes defect 1
  outright): the runtime already enumerates staged repositories in
  `list_models()`; issuing loads for them at server start would make
  raw container restarts self-recovering. Needs design attention:
  load ordering/concurrency on shared GPU memory, interaction with the
  Greengrass lifecycle's own load requests (idempotent, but the
  1800s Startup timeout budget assumes it drives the load), and the
  event-loop blocking below.
- **Non-blocking load path in `vllm_runtime.server`** (defect 4): move
  engine construction off the event loop so status stays observable
  during multi-minute loads.

## Verification

- On-device (ryan-orin-nano/JP6): manual unload -> reload recovery
  validated repeatedly; post-recovery workflow executions green with
  real VLM output.
- Host: deploy_reliability preservation suite (24), dda_triton vLLM
  load-failure suite, vllm_runtime suites including the new reclaim
  tests — all green.
