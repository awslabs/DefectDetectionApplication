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
| pixart-r1 | pixart-alpha + pixart-sigma | g5.xlarge | complete | 2026-08-17T13:27:02Z | 2026-08-17T14:39:24Z | 3.02 | 1.21 |

**Spend so far: USD 1.21 / 500.00** (pixart-r1 complete: 1.2061 instance-hours × $1.006/hr g5.xlarge on-demand)

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
| pixart-alpha | small | `pixart-alpha/pixart-alpha-r1/` | complete | t2i 4/4 ok; 9 inpaint cases `failed/unsupported_task` (T2I-only model per matrix) |
| pixart-sigma | small | `pixart-sigma/pixart-sigma-r1/` | complete | t2i 4/4 ok; 9 inpaint cases `failed/unsupported_task` (T2I-only model per matrix) |
| flux.1-schnell | medium | `flux.1-schnell/` | pending | — |
| flux.1-dev (+Fill) | medium | `flux.1-dev/` | pending | — |
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
