# GPU Quota Audit — Portal_Account 164152369890, us-east-1

**Task 7.1 deliverable** (_Requirements: 3.4_). Produced by
`benchmark-harness/quota_audit.py` (read-only: `service-quotas:ListServiceQuotas`
/ `GetServiceQuota` only — no resource is created, nothing here is
Benchmark_Infrastructure). Consumed by `hosting-comparison.md` (task 7.2) and by
the prerequisites section of `decision-record.md` (task 9.1).

Captured **2026-08-17T23:32:17Z** on Portal_Account 164152369890, us-east-1,
after Phase D teardown was verified clean.

## 1. Live audit output (verbatim)

```
$ cd artifacts/benchmark-harness && python3 quota_audit.py
# GPU Quota Audit — Portal_Account 164152369890, us-east-1

Captured 2026-08-17T23:32:17Z by `benchmark-harness/quota_audit.py` (read-only, Req 3.4).

| Service | Quota | Code | Current value |
|---|---|---|---|
| ec2 | Running On-Demand G and VT instances (vCPUs) | L-DB2E81BA | 768.0 |
| ec2 | Running On-Demand P instances (vCPUs) | L-417A185B | 768.0 |
| sagemaker | ml.g5.xlarge for endpoint usage | L-1928E07B | 4.0 |
| sagemaker | ml.g5.2xlarge for endpoint usage | L-9614C779 | 2.0 |
| sagemaker | ml.g5.4xlarge for endpoint usage | L-C1B9A48D | 2.0 |
| sagemaker | ml.g5.12xlarge for endpoint usage | L-65C4BD00 | 1.0 |
| sagemaker | ml.g6.xlarge for endpoint usage | L-D470D954 | 1.0 |
| sagemaker | ml.g6.2xlarge for endpoint usage | L-98BDE811 | 1.0 |
| sagemaker | ml.g6.4xlarge for endpoint usage | L-DFB10FDE | 1.0 |
| sagemaker | ml.g6.12xlarge for endpoint usage | L-0A29AACF | 1.0 |
| sagemaker | ml.g6e.xlarge for endpoint usage | L-B0729CB4 | 4.0 |
| sagemaker | ml.g6e.2xlarge for endpoint usage | L-F8D7F460 | 4.0 |
| sagemaker | ml.g6e.4xlarge for endpoint usage | L-93531071 | 1.0 |
| sagemaker | ml.g6e.8xlarge for endpoint usage | L-96A28D02 | 1.0 |
| sagemaker | ml.g6e.12xlarge for endpoint usage | L-60313EA3 | 1.0 |
| sagemaker | ml.g6e.16xlarge for endpoint usage | L-2930A179 | 1.0 |
| sagemaker | ml.g6e.24xlarge for endpoint usage | L-AE407E8B | 0.0 |
| sagemaker | ml.g6e.48xlarge for endpoint usage | L-E0C458EA | 0.0 |
| sagemaker | ml.p4d.24xlarge for endpoint usage | L-09F79647 | 0.0 |
| sagemaker | ml.p4de.24xlarge for endpoint usage | L-456B4C5F | 0.0 |
| sagemaker | ml.p5.48xlarge for endpoint usage | L-16AF71F1 | 0.0 |
| sagemaker | ml.g5.xlarge for asynchronous inference endpoint usage | - | not found (default may apply) |
| sagemaker | ml.g6e.xlarge for asynchronous inference endpoint usage | - | not found (default may apply) |
| sagemaker | ml.g6e.2xlarge for asynchronous inference endpoint usage | - | not found (default may apply) |
| sagemaker | ml.g6e.8xlarge for asynchronous inference endpoint usage | - | not found (default may apply) |
```

### 1a. Independent CLI cross-check of the g6e endpoint quotas

```
$ aws service-quotas list-service-quotas --service-code sagemaker --region us-east-1 \
    --query "Quotas[?contains(QuotaName,'g6e') && contains(QuotaName,'endpoint usage')].{Name:QuotaName,Code:QuotaCode,Value:Value}"
[
  {"Name": "ml.g6e.xlarge for endpoint usage",    "Code": "L-B0729CB4", "Value": 4.0},
  {"Name": "ml.g6e.2xlarge for endpoint usage",   "Code": "L-F8D7F460", "Value": 4.0},
  {"Name": "ml.g6e.4xlarge for endpoint usage",   "Code": "L-93531071", "Value": 1.0},
  {"Name": "ml.g6e.8xlarge for endpoint usage",   "Code": "L-96A28D02", "Value": 1.0},
  {"Name": "ml.g6e.12xlarge for endpoint usage",  "Code": "L-60313EA3", "Value": 1.0},
  {"Name": "ml.g6e.16xlarge for endpoint usage",  "Code": "L-2930A179", "Value": 1.0},
  {"Name": "ml.g6e.24xlarge for endpoint usage",  "Code": "L-AE407E8B", "Value": 0.0},
  {"Name": "ml.g6e.48xlarge for endpoint usage",  "Code": "L-E0C458EA", "Value": 0.0}
]
```

### 1b. Note on the "asynchronous inference endpoint usage" rows

Those four rows report `not found`: SageMaker does not publish a separate
per-instance-type *async* endpoint quota under those names on this account.
Async endpoints consume the same per-instance-type **endpoint usage** quota as
real-time endpoints, so the `for endpoint usage` values above are the binding
constraint for both Hosting_Options (SageMaker real-time and SageMaker async).
Treat this as "same quota, no extra request needed", not as "quota 0".

## 2. ⚠️ Correction of an earlier recorded finding (was wrong)

Two Phase C/D documents previously stated that **SageMaker endpoint usage quotas
for g6e instance types are 0** on this account, and used that as the reason
optional task 4.4 (SageMaker cold-start measurement) was skipped. **That
statement was factually wrong.** The live values above show non-zero endpoint
quotas for every g6e type up to `ml.g6e.16xlarge`.

| Instance type | Quota code | Recorded earlier (`quota-audit-trial.md`, 2026-08-17T04:33:54Z) | Live value (2026-08-17T23:32:17Z) |
|---|---|---|---|
| ml.g6e.xlarge | L-B0729CB4 | 0 | **4** (was 1 before the increase below) |
| ml.g6e.2xlarge | L-F8D7F460 | 0 | **4** (was 1 before the increase below) |
| ml.g6e.4xlarge | L-93531071 | 0 | **1** |
| ml.g6e.8xlarge | L-96A28D02 | not audited | **1** |
| ml.g6e.12xlarge | L-60313EA3 | not audited | **1** |
| ml.g6e.16xlarge | L-2930A179 | not audited | **1** |
| ml.g6e.24xlarge | L-AE407E8B | not audited | 0 |
| ml.g6e.48xlarge | L-E0C458EA | not audited | 0 |
| ml.p4d.24xlarge | L-09F79647 | 0 | 0 (unchanged) |

A quota of **1** is sufficient to create a single short-lived endpoint, which is
exactly what task 4.4 needed. **Quota was therefore never the blocker for task
4.4.** Task 4.4 was skipped by an explicit **user decision on 2026-08-17** to
stop provisioning after Phase C/D and proceed to the analysis phases. Both
documents that carried the wrong reason have been corrected:

- `benchmark-results/README.md` — "Optional task 4.4" section
- `benchmark-results/teardown-audit.md` — Step 3 (SageMaker teardown) preamble

The downstream consequence for task 7.2 is unchanged: no SageMaker
scale-from-zero cold start was measured, so `hosting-comparison.md` sources
SageMaker cold-start figures from documented AWS behaviour (clearly labelled as
estimates) plus the Phase C measured model-load proxy.

## 3. Quota increase requests filed during this exploration

Both were filed 2026-08-17 in us-east-1 on account 164152369890 for **production
headroom**, not because the exploration required them.

```
$ aws service-quotas list-requested-service-quota-change-history \
    --service-code sagemaker --region us-east-1
```

| Quota | Code | Request id | Requested | Filed (UTC) | Status | Status verified |
|---|---|---|---|---|---|---|
| ml.g6e.xlarge for endpoint usage | L-B0729CB4 | `5abacbc48f714600aab88fb3048a73300lWtOjBn` | 1 → 4 | 2026-08-17T21:47:21Z | **APPROVED** | 2026-08-17T22:59:29Z |
| ml.g6e.2xlarge for endpoint usage | L-F8D7F460 | `a5045b7c81134c3db082d723129b9dd5KbYC9Qgs` | 1 → 4 | 2026-08-17T21:47:28Z | **APPROVED** | 2026-08-17T22:59:29Z |

Both requests were **pending** when Phase D closed and were **approved** before
this audit ran — which is why `ml.g6e.xlarge` and `ml.g6e.2xlarge` now read 4
rather than the pre-request value of 1. Neither approval was required for any
Phase C run (all Phase C work ran on EC2, not SageMaker), and neither changes
any Phase C measurement. They are recorded as **satisfied prerequisites** in
`decision-record.md`.

## 4. Quota position per size class and Hosting_Option

Instance mapping comes from the Phase C sizing findings
(`benchmark-results/*/run-001/notes.md`): host RAM, not VRAM, was the binding
constraint for the FLUX class — budget ≥64 GiB host RAM per resident FLUX
pipeline (Phase C used g6e.8xlarge with 256 GiB).

| Size class | Model(s) | Hosting shape | Instance type | EC2 quota position | SageMaker endpoint quota |
|---|---|---|---|---|---|
| Small | PixArt-alpha / Sigma | EC2 or SageMaker, resident | g5.2xlarge / g6.2xlarge (≥32 GiB host RAM) | G/VT 768 vCPU — ample | ml.g5.2xlarge 2 · ml.g6.2xlarge 1 |
| Medium | FLUX.1-schnell, FLUX.1-dev + Fill-dev | quantized / offload, single L40S | g6e.2xlarge (64 GiB RAM) | ample | **ml.g6e.2xlarge 4** ✅ |
| Medium | same, resident bf16 (fastest) | ≥64 GiB host RAM + 48 GB VRAM | g6e.4xlarge (128 GiB) / g6e.8xlarge (256 GiB) | ample | ml.g6e.4xlarge 1 · ml.g6e.8xlarge 1 |
| Large | FLUX.2 [dev] NF4 | 4-bit, single L40S | g6e.8xlarge | ample | ml.g6e.8xlarge 1 |
| Large | FLUX.2 [dev] bf16 (production shape) | ≥96 GB VRAM | g6e.12xlarge (4×L40S) or p4d.24xlarge | ample (P 768 vCPU) | ml.g6e.12xlarge 1 · **ml.p4d.24xlarge 0** ❌ |
| Extra-large | HunyuanImage-3.0 bf16 | 3–4×80 GB | p4de.24xlarge / p5.48xlarge | ample | **ml.p4de.24xlarge 0 · ml.p5.48xlarge 0** ❌ |

EC2 quotas are **not** a constraint for anything this exploration recommends:
768 G/VT vCPUs covers 192 concurrent g6e.xlarge or 24 concurrent g6e.8xlarge
instances; 768 P vCPUs covers 8 concurrent p4d.24xlarge.

## 5. Quota increases a future implementation would need

| # | Quota | Code | Current | Needed | Why | Priority |
|---|---|---|---|---|---|---|
| 1 | ml.g6e.2xlarge for endpoint usage | L-F8D7F460 | 4 | ≥4 (satisfied) | prod + dev endpoints per recommended model, plus blue/green replacement during deploys | **satisfied** |
| 2 | ml.g6e.xlarge for endpoint usage | L-B0729CB4 | 4 | ≥4 (satisfied) | smaller/quantized variants, spare capacity | **satisfied** |
| 3 | ml.g6e.4xlarge for endpoint usage | L-93531071 | 1 | 2–4 | resident-bf16 medium-class hosting with blue/green: a rolling endpoint update needs 2 concurrent instances of the type | **required before prod** |
| 4 | ml.g6e.8xlarge for endpoint usage | L-96A28D02 | 1 | 2 | only if the 256 GiB host-RAM shape used in Phase C is carried into production | conditional |
| 5 | ml.p4d.24xlarge / ml.p4de.24xlarge / ml.p5.48xlarge for endpoint usage | L-09F79647 / L-456B4C5F / L-16AF71F1 | 0 | 1+ each | only for a bf16 FLUX.2 [dev] or HunyuanImage-3.0 deployment — neither is recommended, so this is a *deferred* prerequisite | not needed for the recommendation |
| 6 | EC2 G/VT and P on-demand vCPUs | L-DB2E81BA / L-417A185B | 768 / 768 | no change | already ample for every recommended shape | none |

**Lead-time note.** Both increases filed in this exploration went from request to
approval in ≈72 minutes, so per-instance-type SageMaker endpoint increases on
this account are fast — but item 3 should still be filed at the start of the
implementation spec rather than at deploy time, since approval is not guaranteed
to be that quick.

## 6. Provenance

- Script: `benchmark-harness/quota_audit.py` (extended in this task to cover
  g6e.8xlarge–48xlarge, g5/g6 2xlarge–12xlarge, p4de/p5, and the async endpoint
  quota names).
- Superseded earlier capture: `benchmark-harness/quota-audit-trial.md`
  (2026-08-17T04:33:54Z) — retained for the audit trail; its g6e values are
  **superseded by this document** (see §2).
