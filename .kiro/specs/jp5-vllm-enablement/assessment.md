# JP5 vLLM Enablement — Feasibility Assessment (future todo)

> Status: **ASSESSMENT / NOT SCHEDULED**. This document captures the analysis and a spike-first plan for adding vLLM (LLM serving) support to JetPack 5 devices. It is intentionally decision-gated: the first task is a hardware spike that determines whether the rest of the work is viable before any product code is written.

## Motivation

We have JetPack 5 devices with 64 GB RAM (almost certainly **AGX Xavier 64 GB**, compute capability **sm_72 / Volta**) that cannot run JetPack 6. Their large memory makes them attractive for LLM testing, but vLLM currently serves only on `arm64_jp6`. This assessment scopes what it would take to serve vLLM models on `arm64_jp5`.

## What is already in place (the easy 80%)

The vllm-triton-inference and jp6-vllm-enablement features already built everything **except the JP5 device image**:

- **Portal is arch-parametric.** Registration, packaging (`workflow_packaging.py`, `packaging.py`), and deployment gating (`deployments.py`) already say "include `arm64_jp5` only where implemented." No portal redesign is needed — just flipping JP5 into the supported set once the device image works. Defect F/G work made the multi-arch dependency emission correct.
- **Device software is arch-agnostic.** The companion `vllm_runtime` (`VllmRuntimeManager` + loopback server), the `Text_Generation_API` (`endpoints/text_generation.py`), and the `llm_inference` workflow node are plain Python in `src/backend` — they run wherever `import vllm` succeeds. `app.py`'s capability probe (`importlib.util.find_spec("vllm")`) activates them automatically.
- **JP5 image already has the vLLM layer scaffold.** `src/backend/Dockerfile.jp5` carries the same build-arg-gated vLLM layer as JP6 (`VLLM_ENABLE=0`, `VLLM_SPEC`, `VLLM_INDEX_URL`), currently defaulted off. `JP5_VLLM_ENABLED` catalog flag is `False`.

So the entire remaining problem is: **can we produce a JP5 LocalServer image where `import vllm` works and actually serves on Xavier hardware?** Everything upstream and downstream already exists.

## The hard 20%: the JP5 device-image compatibility matrix

This is where the risk lives, and why this is a spike-gated assessment rather than a scheduled feature.

| Dimension | JP6 (working) | JP5 (target) | Risk |
|---|---|---|---|
| L4T / base | l4t-jetpack r36.4.x | **r35.4.1** (current pin) | — |
| CUDA | 12.6 | **11.4** | vLLM/torch wheels for cu114 are old/scarce |
| GPU arch | Orin sm_87 (Ampere) | **Xavier sm_72 (Volta)** | **HIGHEST RISK** — prebuilt attention/quant kernels often target sm_80+; Xavier may be unsupported by any usable vLLM build |
| Prebuilt wheel | `vllm==0.10.2+cu126` from jetson-ai-lab jp6/cu126 (cp310) | Need a **jp5/cu114 vLLM wheel** — existence + version unknown | May not exist at a usable version, or only very old vLLM |
| Interpreter | 3.10 backend / 3.11 tooling dual-interpreter | JP5 image runs 3.11; a JP5 wheel might be cp38/cp310 → **another dual-interpreter split** | Repeats the JP6 interpreter complexity, possibly worse |
| torch pin | supplied by jp6 index | a cu114 torch that the vLLM wheel needs, without clobbering the DDA torch/onnx surface | dependency-surface conflict |

### The gating unknown

**Does a vLLM wheel exist for JP5/CUDA 11.4 that (a) supports Xavier sm_72 and (b) is a recent-enough vLLM to carry the classic `AsyncLLMEngine`/`AsyncEngineArgs`/`SamplingParams` API the companion runtime is written against?**

If yes → the work is a scaled-down repeat of jp6-vllm-enablement (base already fine at r35.4.1, swap the index/pin/interpreter, preserve the vision stack, on-hardware validate).
If no → options degrade to: (1) build vLLM from source for sm_72/cu114 (large, uncertain, possibly blocked by kernel support), (2) an older/alternate LLM runtime on JP5 (llama.cpp / MLC-LLM / TensorRT-LLM) behind the same `Text_Generation_API` contract, or (3) **do not support vLLM on JP5** and document Xavier as vision-only for LLMs. This decision is the spike's output.

## Recommendation

Run a **time-boxed hardware spike** on one of the 64 GB Xavier devices before committing. The spike answers the gating unknown cheaply (a manual `pip install` + `import vllm` + a one-model generate, on-device, no product code). Only if the spike is green do we schedule the implementation phases. This mirrors how jp6-vllm-enablement was ultimately proven — the wheel/index/interpreter reality drove the whole design.

## Coexistence note

Even with the vision stack preserved, a 7B model at fp16 is ~14 GB weights + KV cache; on 64 GB Xavier that comfortably coexists with vision models, so memory is NOT the constraint — kernel/wheel compatibility is. `gpu_memory_utilization` guidance would differ from Orin because Xavier memory bandwidth and the lack of newer tensor-core paths make throughput materially lower; set expectations that JP5 vLLM is for **testing/functional validation, not production throughput**.

## Out of scope for this assessment

- Any change to portal registration/packaging/deployment beyond flipping the JP5 supported-set flag once proven.
- JetPack 4 (Nano/TX2-class) — no vLLM, ever; explicitly excluded.
- Production performance tuning on Xavier.

See `tasks.md` for the spike-first phased plan.
