# Benchmark Results — Run Index and Cost Ledger

Exploration: `opensource-generation-models` · Portal_Account 164152369890 · us-east-1
Protocol: `../benchmark-protocol.md` · Resource tag: `exploration=opensource-generation-models`

## Cost Ledger

**Cost_Cap: USD 500** (protocol §6, hard limit for all Benchmark_Infrastructure)

Rows are appended at instance launch (status `launched`, projected cost) and
updated at terminate (status `complete` / `incomplete`, actual cost). The
`should_provision` gate reads this table before every launch; `spend_so_far` =
actual costs of finished rows + projected costs of in-flight rows. Managed by
`../benchmark-harness/ledger.py` (header format must not change).

| run_id | model | instance_type | status | launch_utc | terminate_utc | projected_cost_usd | actual_cost_usd |
|---|---|---|---|---|---|---|---|
| pixart-r1 | pixart-alpha | g5.xlarge | complete | 2026-08-17T13:27:02Z | 2026-08-17T14:39:24Z | 3.02 | 1.21 |
| pixart-r2 | pixart-sigma | g5.xlarge | complete | 2026-08-17T14:50:30Z | 2026-08-17T15:05:39Z | 3.02 | 0.25 |
| flux1-r1 | flux.1-schnell + flux.1-dev(+Fill) | g6e.8xlarge | complete | 2026-08-17T15:27:02Z | 2026-08-17T16:48:41Z | 13.59 | 6.16 |
| large-r1 | flux.2 + hunyuanimage-2.1 | g6e.8xlarge | complete | 2026-08-17T20:27:56Z | 2026-08-17T21:14:08Z | 18.11 | 3.49 |

**Spend so far: USD 11.11 / 500.00** (pixart-r1 complete: 1.2061 h × $1.006/hr = $1.21; pixart-r2 complete: 0.2525 h × $1.006/hr = $0.25, both g5.xlarge; flux1-r1 complete: 1.3608 h × $4.52856/hr = $6.16, g6e.8xlarge — one shared instance for the flux.1-schnell and flux.1-dev(+Fill) runs, terminated 2026-08-17T16:48:41Z and verified; large-r1 complete: 0.7700 h × $4.52856/hr = $3.49, g6e.8xlarge — one shared instance for the flux.2 and hunyuanimage-2.1 runs, instance `i-0a5ecae8136b7dca2` terminated 2026-08-17T21:14:08Z and verified by tag-filtered `describe-instances`. Per-run `metrics.json` split the 0.77 h evenly (0.385 h / $1.74 each); this ledger row is authoritative for actual spend)

Phase D reconciliation: `billing_reconciled_cost_usd` per run and the final
ledger totals are filled from Cost Explorer actuals filtered by the exploration
tag (task 5.2).

## Run Index

One row per planned Benchmark_Run. Status vocabulary: `pending` (not started),
`complete` (metrics.json schema-valid + evidence per protocol §8),
`incomplete` (Cost_Cap reached or aborted — reason required, Req 2.9),
`skipped` (model excluded by the Evaluation_Matrix, Req 1.7 — reason required).

| model | size class | run dir | status | reason |
|---|---|---|---|---|
| pixart-alpha | small | `pixart-alpha/run-001/` | complete | t2i 4/4 ok; 9 inpaint cases `failed/unsupported_task` (T2I-only model per matrix) |
| pixart-sigma | small | `pixart-sigma/run-001/` | complete | t2i 4/4 ok; 9 inpaint cases `failed/unsupported_task` (T2I-only model per matrix) |
| flux.1-schnell | medium | `flux.1-schnell/run-001/` | complete | 13/13 ok (4 t2i via FluxPipeline 4-step; 9 inpaint via community FluxInpaintPipeline — quality gap vs Fill-dev recorded in rubric, not as failures) |
| flux.1-dev (+Fill) | medium | `flux.1-dev/run-001/` | complete | 13/13 ok (4 t2i via FluxPipeline 28-step; 9 inpaint via official FluxFillPipeline / FLUX.1-Fill-dev 50-step) |
| flux.2 [dev] | large | `flux.2/run-001/` | complete | 13/13 ok (4 t2i via Flux2Pipeline 28-step; 9 "inpaint" via the official instruction-editing path — **no mask API exists for FLUX.2 [dev]**, mask parity fails: outside-mask MAE 9.6–67.2 vs 1.0–6.1 for FLUX.1-Fill-dev). Required bitsandbytes NF4 quantization; bf16+cpu_offload OOMed on the 48 GB L40S (`offload-probe-metrics.json`) |
| hunyuanimage | large | `hunyuanimage/run-001/` | complete (**substituted model**) | **Benchmarked HunyuanImage-2.1 (17B), not the matrix's HunyuanImage-3.0 (80B MoE)** — 3.0 bf16 needs p4de/p5-class hardware, outside the Cost_Cap; substitution sanctioned by the matrix's "Sizing note for Task 4.3" option (c). 4/13 ok: 4 t2i ok; 9 inpaint `failed/unsupported_task` (HunyuanImagePipeline is text-to-image only; no Hunyuan inpaint pipeline in diffusers, no documented mask API for 3.0 either). Required NF4; bf16+cpu_offload OOMed (`attempt-1-bf16-offload-metrics.json`) |

## Evidence Files

- `pre-exploration-stacks.json` — pre-exploration CloudFormation stack snapshot
  (225 stacks, captured 2026-08-17T04:33:10Z before any provisioning; Req 9.4).
  `LastUpdatedTime` falls back to `CreationTime` for never-updated stacks so
  the Phase D diff (task 5.3) is well-defined for every stack.
- `teardown-audit.md` — created in Phase D (task 5.1): tag-filtered teardown
  verification queries, stack snapshot diff, `git status` evidence.
- `<model>/<run-id>/` — per-run `config.json`, `metrics.json`, representative
  `outputs/`, `notes.md` with rubric scores (protocol §8).

> **Execution note (2026-08-17, task 4.3):** the large class ran both models
> sequentially on one g6e.8xlarge (ledger row `large-r1`), the same
> cost-sharing pattern as `flux1-r1`. No new provisioning happened after the
> single gated launch: the task was resumed against the already-running tagged
> instance (`should_provision(7.62, 18.11, 500) → True` was the gate for that
> launch), and the duplicate-execution check
> (`describe-instances` + ledger scan) found exactly one in-flight instance,
> which was adopted rather than duplicated. Both model runs had completed on
> the instance; evidence was pulled from the tagged bucket and the instance
> terminated immediately. Neither large-class model reached the Cost_Cap, so no
> run is recorded `incomplete`. Phase G follow-up candidate noted in
> `flux.2/run-001/notes.md`: **FLUX.2 [klein]** (Apache 2.0) is the only FLUX.2
> family member with a real mask inpaint pipeline in diffusers and was not
> benchmarked under this task.
>
> **Reconciliation note (2026-08-17, task 4.1):** two concurrent executions of
> task 4.1 ran against the small-class infrastructure. `pixart-alpha/run-001/`
> and `pixart-sigma/run-001/` (referenced by the run index above) are the
> canonical evidence: alpha from instance `i-01b4e3e5e2fe0eb99` (ledger
> `pixart-r1`), sigma from instance `i-01361c1f436a6cab7` (ledger `pixart-r2`).
> The earlier-committed `pixart-alpha/pixart-alpha-r1/` and
> `pixart-sigma/pixart-sigma-r1/` dirs are a parallel execution's results from
> the shared `pixart-r1` instance; their per-run `instance_hours`/cost fields
> split that instance's 1.2061 h evenly and must not be summed with the
> `run-001` attributions — the cost ledger above is the single source of truth
> for actual spend (USD 1.46 total across both instances). Task 5.2
> reconciliation should collapse to one metrics set per model.
