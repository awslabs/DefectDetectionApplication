# JP5 vLLM Enablement — Phased Plan (future todo, spike-gated)

> NOT SCHEDULED. Read `assessment.md` first. Phase 0 is a decision gate: do not start Phases 1+ until Phase 0 proves a usable JP5/Xavier vLLM path exists. Each phase names its own exit criteria.

## Phase 0 — Hardware feasibility spike (DECISION GATE, ~1 day, user-gated)

- [ ] 0.1 On a 64 GB Xavier (JP5) device, record the exact platform: `cat /etc/nv_tegra_release` (L4T version), CUDA version, default `python3` version, GPU compute capability (`sm_72` expected). Confirm the device is AGX Xavier, not Orin.
- [ ] 0.2 Survey prebuilt wheels: check `https://pypi.jetson-ai-lab.io/jp5/cu114` (and any current jp5 channel) for a `vllm` wheel and its companion `torch`. Record: does a vLLM wheel exist? which vLLM version? which cpXX (interpreter)? which torch pin? does it claim sm_72 support?
- [ ] 0.3 In a throwaway container FROM the current JP5 base (`l4t-jetpack:r35.4.1`), attempt `pip install` of the discovered vLLM wheel + torch under a matching interpreter. Record success/failure and the resolved dependency set.
- [ ] 0.4 If install succeeds: `import vllm`, instantiate `AsyncEngineArgs`/`AsyncLLMEngine.from_engine_args` for `facebook/opt-125m`, run one `generate()` on the Xavier GPU. Confirm the classic API surface the companion runtime needs is present, and that kernels actually run on sm_72 (watch for "no kernel image is available for execution on the device").
- [ ] 0.5 **Decision**: record one of GREEN (usable wheel + API + sm_72 kernels run) / AMBER (works only via source build or an older vLLM missing API) / RED (no viable path). This determines whether Phases 1–4 proceed, pivot to an alternate runtime (Phase 3-ALT), or stop.
- Exit criteria: a written GREEN/AMBER/RED verdict with the exact wheel/version/interpreter/torch facts, appended to assessment.md.

## Phase 1 — Requirements + design (only if Phase 0 is GREEN or AMBER-with-plan)

- [ ] 1.1 Write requirements.md modeled on jp6-vllm-enablement: JP5 image serves vLLM by default-or-flag; dual-interpreter handling if the wheel is not cp311; base/CUDA kept at r35.4.1/cu114 (no base bump unless the spike says otherwise); **vision-stack preservation** (all existing JP5 vision model conversion/loading/inference byte-identical); portal JP5 gating flip; build/test gates stay green; on-hardware validation.
- [ ] 1.2 Write design.md: the exact `Dockerfile.jp5` vLLM layer values (index/pin/interpreter), the interpreter strategy (reuse JP6's dual-interpreter pattern if the wheel is cp310/cp38), torch dependency-surface reconciliation, and the Xavier `gpu_memory_utilization`/`max_model_len` guidance.
- Exit criteria: requirements + design reviewed; the interpreter and wheel decisions are concrete, not TBD.

## Phase 2 — JP5 image build enablement

- [ ] 2.1 Set `Dockerfile.jp5` vLLM layer defaults (`VLLM_ENABLE=1`, `VLLM_SPEC`=the proven pin, `VLLM_INDEX_URL`=the jp5 index) mirroring the JP6 layer; thread `PYTHON_VERSION` if a dual-interpreter split is needed.
- [ ] 2.2 Preserve the vision stack: CUDA 11.4 cudart/Neo-DLR resolution, Triton stub stack, onnxruntime-gpu, aravis/GStreamer from-source steps must all still build and load on the (unchanged) r35.4.1 base.
- [ ] 2.3 Recapture the JP5 Docker preservation goldens (masked bytes + default-refs) for the intended changes ONLY; keep JP6/x86/JP4 goldens byte-identical. Re-run the six security audit gates and the interpreter audit.
- Exit criteria: a JP5 image builds with `import vllm` succeeding under the DDA interpreter; all build/security/interpreter gates green; goldens reflect only intended deltas.

## Phase 3 — Portal JP5 gating flip

- [ ] 3.1 Flip `JP5_VLLM_ENABLED` (catalog) to True and include `arm64_jp5` in the vLLM component supported-set where the packaging/deploy gates already parametrize it. Verify the Defect F/G multi-variant + vision `published_components` logic handles a JP5+JP6 vLLM model correctly (single-variant emit / fail-closed coverage).
- [ ] 3.2 Portal + workflow_core + frontend test suites green; add JP5 to the arch-gating tests.
- Exit criteria: a vLLM model packages for arm64_jp5 and the deploy gate accepts a JP5 device.

### Phase 3-ALT — Alternate runtime (only if Phase 0 = AMBER/RED but LLM-on-JP5 still wanted)

- [ ] 3-ALT.1 Evaluate llama.cpp / MLC-LLM / TensorRT-LLM on Xavier behind the SAME `Text_Generation_API` contract (so `llm_inference` node, portal, and packaging are unchanged). This is a separate design; the device runtime becomes a non-vLLM backend implementing the generate/stream interface. Larger effort; only pursue if there is real demand for LLM-on-Xavier and vLLM proper is RED.

## Phase 4 — On-hardware validation (user-gated)

- [ ] 4.1 Document + run the manual validation on the 64 GB Xavier: register (opt-125m smoke + a mid-size model), package for arm64_jp5, publish, deploy, READY propagation, non-streaming generate, SSE stream, an `llm_inference` workflow run, and **vision+vLLM coexistence** on the same device. Capture Xavier throughput expectations (functional, not production).
- Exit criteria: end-to-end green on real Xavier hardware; results documented; then commit with the usual on-hardware verification statement.

## Notes

- Device-side changes ride a LocalServer JP5 build (~same cost as the JP6 build cadence). Build one component at a time per builds.md.
- The single biggest determinant of scope is Phase 0.2/0.4 (wheel existence + sm_72 kernel execution). Everything else is a scaled repeat of jp6-vllm-enablement, which is a known quantity.
- Keep JP4 and x86 untouched throughout.
