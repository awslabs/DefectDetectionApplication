# Benchmark Protocol — Open-Source Generation Models Exploration

**Spec:** `.kiro/specs/opensource-generation-models/`
**Account / Region:** Portal_Account 164152369890, us-east-1
**Resource tag (mandatory on every provisioned resource):** `exploration=opensource-generation-models`
**Status:** Committed before any Benchmark_Infrastructure is provisioned (Req 2.1 gate).

This protocol defines how each Candidate_Model is benchmarked on temporary GPU
infrastructure. It is the provisioning gate: no Benchmark_Infrastructure may be
created before this document is committed, the harness tests pass, and the
pre-exploration CloudFormation stack snapshot exists.

---

## 1. Candidate List

The candidate set is **parameterized over the Evaluation_Matrix outcome**
(`artifacts/evaluation-matrix.md`). The full pre-exclusion candidate set is:

| # | Candidate_Model | Size class | Planned instance |
|---|---|---|---|
| 1 | FLUX.1-dev (+ FLUX.1-Fill-dev for inpainting) | medium (~12B) | g6e.xlarge |
| 2 | FLUX.1-schnell | medium (~12B) | g6e.xlarge |
| 3 | FLUX.2 | large (TBD by matrix) | g6e.2xlarge / g6e.4xlarge (p4d slice only if required) |
| 4 | HunyuanImage | large (TBD by matrix) | g6e.2xlarge / g6e.4xlarge (p4d slice only if required) |
| 5 | PixArt-alpha | small (~0.6B) | g5.xlarge / g6.xlarge |
| 6 | PixArt-Sigma | small (~0.6B) | g5.xlarge / g6.xlarge |

**Exclusion rule (Req 1.7):** any Candidate_Model marked
`excluded (weights unobtainable)` in the finalized Evaluation_Matrix is removed
from this list before Phase C begins, and the skip is recorded in the run index
(`benchmark-results/README.md`). The per-run procedure, metrics, rubric, cost
cap, and teardown steps below apply identically to whatever candidate subset
survives the matrix — no step in this protocol depends on the exact membership
of the candidate set.

Large-class instance sizing (row 3–4) is finalized from the parameter counts
the Evaluation_Matrix pins; until then the g6e.2xlarge / g6e.4xlarge range is
the planning assumption.

## 2. Frozen Test-Case Set

The frozen inputs are committed under `artifacts/benchmark-harness/cases/` with
manifest `cases.json` (fields per case: `case_id`, `task_type`, `prompt`,
`seed`, `image` / `mask` relative paths for inpainting cases). The same
byte-identical files and manifest run against every candidate. Seeds are fixed
per case so reruns are comparable.

### 2.1 Inpainting cases (primary path) — 9 source/mask/prompt triples

Two source-image families, both 768×768 PNG with binary masks (white = region
to inpaint), deliberately varying defect type and mask size:

- **Synthetic industrial textures** (inpaint-001…006), generated
  deterministically by `cases/generate_cases.py`.
- **Real production imagery** (inpaint-101…103): cookie source images copied
  read-only from the portal's training data
  (`s3://ryvan-cookies/training-images/normal-*.jpg`), deterministically
  center-cropped and resized by `cases/prepare_cookie_cases.py`. These match
  the Synthetic_Data_Generator's actual usage domain.

| case_id | Defect type | Mask shape / size | Seed |
|---|---|---|---|
| inpaint-001 | scratch on brushed metal plate | thin diagonal rectangle (small, ~3% area) | 101 |
| inpaint-002 | corrosion patch on steel sheet | ellipse (medium, ~9% area) | 102 |
| inpaint-003 | dent on stamped panel | circle (small-medium, ~4% area) | 103 |
| inpaint-004 | weld porosity on seam | narrow horizontal band (medium, ~6% area) | 104 |
| inpaint-005 | paint chip on coated surface | irregular rectangle (large, ~19% area) | 105 |
| inpaint-006 | crack in cast housing | long thin rectangle (small, ~2% area) | 106 |
| inpaint-101 | broken edge on cookie (real photo) | rim wedge (large, ~11% area) | 111 |
| inpaint-102 | burned patch on cookie (real photo) | off-center ellipse (medium, ~8% area) | 112 |
| inpaint-103 | crack across cookie (real photo) | long thin diagonal (small, ~2% area) | 113 |

Prompts follow the pipeline's defect-prompt shape (e.g., "a deep scratch on a
brushed metal bracket, industrial inspection photo"); exact prompt strings live
in `cases.json` and are authoritative.

### 2.2 Text-to-image cases — 4 defect prompts

| case_id | Prompt theme | Seed |
|---|---|---|
| t2i-001 | scratched metal plate, top-down inspection photo | 201 |
| t2i-002 | corroded steel surface with rust patches | 202 |
| t2i-003 | cracked plastic housing, macro defect photo | 203 |
| t2i-004 | broken cookie on conveyor belt, factory inspection photo | 204 |

### 2.3 Immutability

The `cases/` directory is frozen once committed. Any change to a case after the
first Benchmark_Run invalidates cross-model comparability and requires
rerunning all completed runs (subject to the Cost_Cap) or documenting the
discontinuity in the run index.

## 3. Per-Run Procedure

One Benchmark_Run = one Candidate_Model on one instance type. Steps:

1. **Gate check.** Read the cost ledger (`benchmark-results/README.md`);
   compute `projected_run_cost` = per-run wall-clock budget (below) × on-demand
   hourly rate. Proceed only if `should_provision(spend_so_far,
   projected_run_cost, cap)` returns true (harness pure function, unit-tested).
2. **Ledger update (pre-launch).** Append a ledger row with run_id, model,
   instance type, projected cost, status `launched`, launch timestamp.
3. **Launch.** Start one on-demand EC2 instance (Deep Learning AMI, us-east-1)
   with tag `exploration=opensource-generation-models` and the self-terminating
   user-data guard (`benchmark-harness/user_data.sh.tmpl`): a cron-scheduled
   `shutdown` fires after the per-run wall-clock budget (default **3 hours**,
   large class **4 hours**) so an abandoned instance cannot silently burn
   budget. Record the launch timestamp.
4. **Load.** Install/activate the pinned environment (torch + diffusers), load
   the model via its official `diffusers` pipeline, record
   `model_load_seconds` (start of pipeline load to pipeline ready).
5. **Run cases.** Execute every case from `cases.json` in manifest order via
   the harness case loop: fixed seed per case, record per-case
   `latency_seconds`, `status`, `output_uri`; on any case failure record
   `status: failed` + `failure_mode` and continue with remaining cases
   (Req 2.10). Inpainting cases run only where the Evaluation_Matrix records an
   inpainting path; unsupported cases are recorded as
   `failed / failure_mode: unsupported_task`.
6. **Capture metrics.** Write `metrics.json` (schema §4); the harness schema
   assertion must pass before the run is marked complete. Upload outputs to the
   tagged benchmark S3 bucket; copy representative outputs into
   `benchmark-results/<model>/<run-id>/outputs/`.
7. **Terminate.** Terminate the instance; record the terminate timestamp;
   update the ledger row with actual instance-hours and
   `estimated_cost_usd = instance_hours × on-demand rate`, status `complete`.
8. **Score.** Human-score every inpainting output against the rubric (§5) into
   `benchmark-results/<model>/<run-id>/notes.md`.

**Cold_Start_Time (Phase C definition):** `model_load_seconds` + first-case
latency on a fresh instance. Hosting-option-specific cold starts (SageMaker
scale-from-zero) are measured only under optional task 4.4 and otherwise
estimated from documentation in the hosting comparison.

## 4. Metrics Definitions

One `metrics.json` per run, schema enforced by the harness before completion:

```json
{
  "run_id": "…", "model": "…", "instance_type": "…",
  "account": "164152369890", "region": "us-east-1",
  "model_load_seconds": 0.0,
  "cases": [
    {"case_id": "…", "task_type": "inpainting|text_to_image",
     "latency_seconds": 0.0, "seed": 0, "output_uri": "…",
     "status": "ok|failed", "failure_mode": null}
  ],
  "instance_hours": 0.0, "estimated_cost_usd": 0.0,
  "billing_reconciled_cost_usd": null
}
```

- **Per-image latency** (`latency_seconds`): wall-clock seconds from pipeline
  invocation to image bytes available, per case, steady-state (model already
  loaded).
- **model_load_seconds**: pipeline load start → pipeline ready on a fresh
  instance.
- **Cold_Start_Time**: `model_load_seconds` + first-case latency (proxy, §3).
- **Actual cost**: `estimated_cost_usd` from instance-hours × published
  on-demand rate at run time; `billing_reconciled_cost_usd` filled in Phase D
  from Cost Explorer filtered by the exploration tag (starts `null`).
- **output_uri**: S3 URI in the benchmark bucket (representative copies
  committed under `benchmark-results/`); `null` for failed cases.

## 5. Human Quality Rubric (inpainting outputs)

Each inpainting output is scored 1–5 on four axes, recorded in the run's
`notes.md`:

| Axis | 1 | 5 |
|---|---|---|
| **Mask adherence** | defect spills far outside the mask | defect entirely inside the mask |
| **Background preservation** | background outside mask visibly altered | background pixel-faithful outside mask |
| **Defect realism** | not recognizable as the prompted defect | photorealistic, plausible industrial defect |
| **Prompt fidelity** | ignores the prompt | matches defect type and description precisely |

The rubric is authoritative over any automated metric (the pipeline's consumer
is a human approving previews). Automated metrics (e.g., LPIPS outside the mask
vs source) may be captured where cheap and noted in `notes.md` but do not
override rubric scores.

## 6. Cost_Cap and Ledger Procedure

- **Cost_Cap: USD 500** for all Benchmark_Infrastructure combined (final,
  per design proposal).
- The ledger is the cost table in `benchmark-results/README.md`, updated at
  every launch (projected) and terminate (actual).
- Before every provisioning action: `should_provision(spend_so_far,
  projected_run_cost, cap)` must be true, where `spend_so_far` is the sum of
  actual costs of finished runs plus projected costs of in-flight runs. The
  function returns false whenever `spend + projected >= cap`.
- If the ledger reaches the cap: provisioning stops immediately; remaining runs
  are recorded `incomplete` with reason in the run index (Req 2.9); downstream
  phases proceed on partial data.
- Phase D reconciles the ledger against Cost Explorer actuals filtered by the
  exploration tag before the Cost_Model is finalized.

## 7. Teardown Checklist (7 steps, Req 2.8 / 9.3)

1. Terminate all EC2 instances tagged `exploration=opensource-generation-models`;
   verify `describe-instances` with the tag filter returns no non-terminated
   instances.
2. Delete unattached EBS volumes and snapshots carrying the tag.
3. Delete any SageMaker endpoints / endpoint-configs / models carrying the tag
   (if the optional shortlist measurement ran); verify list calls return empty.
4. Copy representative outputs into `benchmark-results/`, then delete the
   benchmark S3 bucket.
5. Delete temporary security groups / key pairs / IAM roles created for the
   benchmark.
6. Capture the CLI output of every tag-filtered verification query into
   `benchmark-results/teardown-audit.md`. If any tagged resource survives,
   teardown is not confirmed — re-run until all queries return empty.
7. Portal-untouched verification: diff the post-exploration
   `aws cloudformation describe-stacks` snapshot (stack names +
   `LastUpdatedTime`) against the pre-exploration snapshot
   (`benchmark-results/pre-exploration-stacks.json`) — must be empty — and run
   `git status` over `edge-cv-portal/` and `src/` — must show no modifications
   (Req 9.2, 9.4).

## 8. Evidence Requirements

A Benchmark_Run is complete only when all of the following exist:

- `benchmark-results/<model>/<run-id>/config.json` — instance type, AMI,
  environment pins, per-run wall-clock budget, launch/terminate timestamps.
- `benchmark-results/<model>/<run-id>/metrics.json` — schema-valid (§4), every
  attempted case has latency/seed/status and (on success) output reference;
  instance-hours and estimated cost present.
- `benchmark-results/<model>/<run-id>/outputs/` — representative generated
  images (full sets stay in the benchmark bucket until teardown, referenced by
  `output_uri`).
- `benchmark-results/<model>/<run-id>/notes.md` — rubric scores for every
  inpainting case (inpainting-capable models), observations, failure notes.
- Updated ledger row in `benchmark-results/README.md`.

Exploration-level evidence: the pre/post stack snapshots, the reconciled
ledger, and `teardown-audit.md` with all tag-filtered queries returning empty.
