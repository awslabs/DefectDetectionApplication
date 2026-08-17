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

**FINAL TOTAL (all runs complete, teardown verified): USD 11.11 / 500.00 Cost_Cap
— 2.2 % of cap used, no run stopped for cost.** Sum of the four `actual_cost_usd`
values above (1.21 + 0.25 + 6.16 + 3.49). Per-model attribution (one metrics set
per model, shared instances split evenly — see the reconciliation note below):
pixart-alpha $1.21 · pixart-sigma $0.25 · flux.1-schnell $3.08 ·
flux.1-dev(+Fill) $3.08 · flux.2 $1.74 · hunyuanimage-2.1 $1.74.

**Spend derivation: USD 11.11 / 500.00** (pixart-r1 complete: 1.2061 h × $1.006/hr = $1.21; pixart-r2 complete: 0.2525 h × $1.006/hr = $0.25, both g5.xlarge; flux1-r1 complete: 1.3608 h × $4.52856/hr = $6.16, g6e.8xlarge — one shared instance for the flux.1-schnell and flux.1-dev(+Fill) runs, terminated 2026-08-17T16:48:41Z and verified; large-r1 complete: 0.7700 h × $4.52856/hr = $3.49, g6e.8xlarge — one shared instance for the flux.2 and hunyuanimage-2.1 runs, instance `i-0a5ecae8136b7dca2` terminated 2026-08-17T21:14:08Z and verified by tag-filtered `describe-instances`. Per-run `metrics.json` split the 0.77 h evenly (0.385 h / $1.74 each); this ledger row is authoritative for actual spend)

### Phase D billing reconciliation (task 5.2) — **PENDING-WITH-EVIDENCE**

`billing_reconciled_cost_usd` remains `null` in every `metrics.json`. Tag-filtered
Cost Explorer actuals were **not yet available** when reconciliation ran; the
figures above are therefore not replaced by invented numbers. Each canonical
`metrics.json` carries a `billing_reconciliation` object recording the status,
the exact query, the timestamp, and the ledger row it will reconcile against.

Attempted 2026-08-17T21:29:43Z (last benchmark instance terminated
2026-08-17T21:14:08Z — 15 minutes earlier):

```
$ aws ce get-cost-and-usage --time-period Start=2026-08-17,End=2026-08-19 \
    --granularity DAILY --metrics UnblendedCost \
    --filter '{"Tags":{"Key":"exploration","Values":["opensource-generation-models"]}}' \
    --group-by Type=DIMENSION,Key=SERVICE
→ 2026-08-17: Total UnblendedCost 0 USD, Groups [], Estimated true
→ 2026-08-18: Total UnblendedCost 0 USD, Groups [], Estimated true
```

Two independent reasons the tag-filtered actuals are empty, both confirmed:

1. **The `exploration` tag is not an activated cost allocation tag.**
   `aws ce get-tags --time-period Start=2026-08-17,End=2026-08-19 --tag-key exploration`
   returns `{"Tags": [""], "ReturnSize": 1, "TotalSize": 1}` and
   `aws ce list-cost-allocation-tags` returns `{"CostAllocationTags": []}` — the
   account has no cost allocation tag activated, so Cost Explorer cannot split
   spend by this tag at all. Activation is a billing-console action that also
   only applies to usage recorded *after* activation, so retroactive
   tag-filtered reconciliation of these runs is not achievable on this account.
2. **Cost Explorer has not ingested the GPU usage yet.** An untagged control
   probe for the exact benchmark usage types on the run day returned no rows:
   `--filter` on `USAGE_TYPE in {BoxUsage:g5.xlarge, BoxUsage:g6e.8xlarge}`,
   `REGION=us-east-1`, `2026-08-17` → no groups, while the same query *did*
   return `EBS:VolumeUsage.gp3` (1.8047 USD) and `TimedStorage-ByteHrs`
   (0.2882 USD) for that day. So 2026-08-17 is only partially ingested (CE lags
   several hours to ~24 h and the day is still `Estimated: true`).

**Consequence.** The cost ledger table above is the **authoritative** cost
record for this exploration (instance-hours from the recorded launch/terminate
timestamps × the published us-east-1 on-demand rate). Phase E's cost model
should cite the ledger, not Cost Explorer. To close the reconciliation later,
re-run the query above once 2026-08-17/18 are finalized in Cost Explorer;
absent tag activation, the usable check is the untagged
`BoxUsage:g5.xlarge` + `BoxUsage:g6e.8xlarge` totals for 2026-08-17, which
should come to ≈ USD 11.11 (1.4586 g5.xlarge hours + 2.1308 g6e.8xlarge hours),
allowing for any unrelated account usage of those types.

**Duplicate-attribution collapse (as instructed by the reconciliation note at
the bottom of this file):** one metrics set per model is now explicit.
`pixart-alpha/run-001/` and `pixart-sigma/run-001/` are canonical;
`pixart-alpha/pixart-alpha-r1/` and `pixart-sigma/pixart-sigma-r1/` are marked
`SUPERSEDED.md` + `billing_reconciliation.status = "superseded-not-attributed"`
and their hours/costs are excluded from every total (they are retained only
because their `outputs/` images are the last surviving copies of that parallel
execution). The even 50 % splits recorded inside the shared-instance runs
(`flux1-r1` → flux.1-schnell + flux.1-dev; `large-r1` → flux.2 +
hunyuanimage-2.1) are per-model attributions of one real instance cost, not
additional spend: 3.08 + 3.08 = 6.16 (`flux1-r1`) and 1.74 + 1.74 = 3.49
(`large-r1`, ±0.01 rounding).

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

### Optional task 4.4 — SageMaker cold start: **SKIPPED by user decision (2026-08-17)**

No SageMaker endpoint was ever created for this exploration. The reason is an
explicit **user decision to stop provisioning** after Phase C/D and move to the
analysis phases (Phases E–G). It was neither a Cost_Cap constraint (USD 488.89
of the cap was still free) nor a quota constraint. Task 4.4 is optional (`*` in
`tasks.md`), so skipping it is within the plan.

> **⚠️ Correction (task 7.1, 2026-08-17T23:32:17Z).** An earlier version of this
> section stated that SageMaker endpoint usage quotas for g6e instance types were
> **0** on this account and gave that as the reason task 4.4 could not run. **That
> was factually wrong.** A live re-audit
> (`aws service-quotas list-service-quotas --service-code sagemaker --region
> us-east-1`, full transcript in `../quota-audit.md`) shows non-zero endpoint
> usage quotas for every g6e type up to 16xlarge: `ml.g6e.xlarge` L-B0729CB4 = 4,
> `ml.g6e.2xlarge` L-F8D7F460 = 4 (both raised from **1** by increases approved
> 2026-08-17T22:59:29Z), `ml.g6e.4xlarge` L-93531071 = 1, `ml.g6e.8xlarge`
> L-96A28D02 = 1, `ml.g6e.12xlarge` L-60313EA3 = 1, `ml.g6e.16xlarge`
> L-2930A179 = 1; only `ml.g6e.24xlarge` L-AE407E8B, `ml.g6e.48xlarge`
> L-E0C458EA and the p4d/p4de/p5 types are 0. A quota of **1 is sufficient for a
> single short-lived cold-start measurement endpoint**, so **quota was not the
> blocker**. The stale `0` values came from
> `../benchmark-harness/quota-audit-trial.md` (2026-08-17T04:33:54Z), which is now
> marked superseded for its g6e rows.

**Consequence for Phase E (task 7.2, hosting comparison) — unchanged:** because
no endpoint was created, SageMaker scale-from-zero cold start is sourced from
**documented AWS behaviour, clearly labelled as estimates** rather than
measurements, combined with the measured Phase C model-load proxy from protocol
§3 (`model_load_seconds` + first-case latency on a fresh EC2 instance), available
per run in each `metrics.json`. Measuring real SageMaker scale-from-zero cold
start remains an open item for the future implementation spec (recorded in
`../decision-record.md`), not a blocked one.

## Evidence Files

- `pre-exploration-stacks.json` — pre-exploration CloudFormation stack snapshot
  (225 stacks, captured 2026-08-17T04:33:10Z before any provisioning; Req 9.4).
  `LastUpdatedTime` falls back to `CreationTime` for never-updated stacks so
  the Phase D diff (task 5.3) is well-defined for every stack.
- `post-exploration-stacks.json` — post-exploration CloudFormation stack
  snapshot (225 stacks, captured 2026-08-17T21:31:09Z after teardown; Req 9.4),
  same shape as the pre-exploration snapshot.
- `teardown-audit.md` — Phase D evidence (tasks 5.1 and 5.3): all 7 protocol §7
  teardown steps with literal CLI transcripts, the tag-filtered verification
  sweep (all per-service queries empty — Property 6), the pre/post stack
  snapshot diff with per-stack attribution, and `git status` over
  `edge-cv-portal/` and `src/` (Property 7). Teardown result: benchmark bucket,
  security group, IAM role + instance profile, and the temporary HF-token SSM
  parameter deleted; no instance, volume, snapshot, key pair, or SageMaker
  resource left; 0 stacks added or removed.
- `<model>/<run-id>/` — per-run `config.json`, `metrics.json`, representative
  `outputs/`, `notes.md` with rubric scores (protocol §8), plus the run's
  driver log pulled from the bucket before deletion. Every committed output was
  byte-verified (md5 vs S3 ETag) against the bucket object before the bucket was
  deleted; the `output_uri` values in `metrics.json` now point at deleted
  objects by design (protocol §7.4) — the committed `outputs/` are the surviving
  evidence. Coverage: 4/4 attempted cases for the three text-to-image-only
  models, 5/13 representative cases (same case ids across models) for the FLUX
  runs.

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
> **Done (task 5.2):** the two parallel-execution dirs now carry `SUPERSEDED.md`
> and `billing_reconciliation.status = "superseded-not-attributed"`; the
> canonical set is one `run-001/` per model. See the billing reconciliation
> section above.
>
> **Phase D closure (2026-08-17, tasks 5.1 / 5.2 / 5.3):** teardown executed and
> verified — see `teardown-audit.md`. All tagged Benchmark_Infrastructure is
> gone (bucket, security group, IAM role + instance profile, temporary HF-token
> SSM parameter; instances/volumes already gone), every per-service tag-filtered
> query returns empty, no CloudFormation stack was added or removed, and
> `git status` over `edge-cv-portal/` and `src/` is clean. Final ledger total
> USD 11.11 of the 500 Cost_Cap; billing reconciliation is
> **pending-with-evidence** (Cost Explorer tag data unavailable — details above),
> with the ledger table as the authoritative cost source for Phase E.
