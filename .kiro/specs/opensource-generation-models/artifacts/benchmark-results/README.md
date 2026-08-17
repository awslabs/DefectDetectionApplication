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

**Spend so far: USD 7.62 / 500.00** (pixart-r1 complete: 1.2061 h × $1.006/hr = $1.21; pixart-r2 complete: 0.2525 h × $1.006/hr = $0.25, both g5.xlarge; flux1-r1 complete: 1.3608 h × $4.52856/hr = $6.16, g6e.8xlarge — one shared instance for the flux.1-schnell and flux.1-dev(+Fill) runs, terminated 2026-08-17T16:48:41Z and verified)

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
| flux.2 | large | `flux.2/` | pending | — (inclusion per Evaluation_Matrix) |
| hunyuanimage | large | `hunyuanimage/` | pending | — (inclusion per Evaluation_Matrix) |

## Evidence Files

- `pre-exploration-stacks.json` — pre-exploration CloudFormation stack snapshot
  (225 stacks, captured 2026-08-17T04:33:10Z before any provisioning; Req 9.4).
  `LastUpdatedTime` falls back to `CreationTime` for never-updated stacks so
  the Phase D diff (task 5.3) is well-defined for every stack.
- `teardown-audit.md` — created in Phase D (task 5.1): tag-filtered teardown
  verification queries, stack snapshot diff, `git status` evidence.
- `<model>/<run-id>/` — per-run `config.json`, `metrics.json`, representative
  `outputs/`, `notes.md` with rubric scores (protocol §8).

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
