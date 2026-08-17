# Design Document

## Overview

This design describes an **exploration and planning effort**, not a production feature. The "system" being designed is the exploration itself: where its deliverables live, how the benchmark harness provisions and tears down temporary GPU infrastructure under a Cost_Cap, how the seven deliverables (Evaluation_Matrix, Benchmark_Protocol + results, Hosting comparison, Cost_Model, Model_Registry_Proposal, Integration_Proposal, Decision_Record) are structured, and how the whole effort is verified to leave the portal untouched (Requirement 9).

No portal source code, frontend, or CDK infrastructure is modified. The only AWS resources created are tagged, teardown-covered Benchmark_Infrastructure in the Portal_Account (164152369890, us-east-1).

## Deliverable Layout

All exploration artifacts live inside the spec directory so they travel with the spec and never mix with production code:

```
.kiro/specs/opensource-generation-models/
├── requirements.md
├── design.md                      (this document)
├── tasks.md
└── artifacts/
    ├── evaluation-matrix.md       (Req 1)
    ├── benchmark-protocol.md      (Req 2.1–2.4)
    ├── benchmark-results/
    │   ├── README.md              (index of runs, cost ledger, teardown evidence)
    │   ├── <model>/<run-id>/      (per-run: config.json, metrics.json, outputs/*.png, notes.md)
    │   └── teardown-audit.md      (Req 2.8, 9.3, 9.4 evidence)
    ├── hosting-comparison.md      (Req 3)
    ├── cost-model.md              (Req 4)
    ├── model-registry-proposal.md (Req 5)
    ├── integration-proposal.md    (Req 6)
    └── decision-record.md         (Req 7, 8)
```

Rationale: `docs/exploration` at repo root would suggest long-lived documentation; these artifacts are inputs to a *future implementation spec*, so co-locating them with this spec under `artifacts/` keeps the repo root clean and satisfies Req 9.1 (deliverables are documents and benchmark results only). Large binary outputs (generated images) are kept small in count — a handful of representative images per model; full output sets stay in the benchmark S3 bucket until teardown and are referenced by URI in `metrics.json`, with representative copies committed.

The benchmark harness code (scripts, not portal code) lives in `artifacts/benchmark-harness/` — plain Python scripts run from the engineer's workstation or the benchmark instance, never deployed as portal infrastructure.

## Architecture

### Exploration Phases

```
Phase A: Desk research          → evaluation-matrix.md
Phase B: Protocol authoring     → benchmark-protocol.md   (gate: no provisioning before this exists, Req 2.1)
Phase C: Benchmark execution    → benchmark-results/       (temporary infra, Cost_Cap enforced)
Phase D: Teardown + audit       → teardown-audit.md        (gate: nothing proceeds until clean)
Phase E: Analysis               → hosting-comparison.md, cost-model.md
Phase F: Design proposals       → model-registry-proposal.md, integration-proposal.md
Phase G: Decision               → decision-record.md
```

Phases A and B are order-independent; C strictly follows B (Req 2.1). E–G consume C's measured data. If the Cost_Cap halts C early (Req 2.9), E–G proceed with the data collected, and the Decision_Record notes incomplete runs.

### Benchmark Harness Design (Requirement 2)

**Provisioning approach — single EC2 GPU instance per candidate, `diffusers`-based.**

Chosen over short-lived SageMaker endpoints for the *benchmark* (not for production — that question is what the hosting comparison answers) because:

- One EC2 instance gives direct control over model loading, timing instrumentation, and cost attribution (instance-hours × published rate, cross-checked against Cost Explorer).
- `diffusers` supports all candidates' official pipelines (FluxPipeline, FluxFillPipeline, HunyuanDiT/HunyuanImage pipelines, PixArtAlphaPipeline / PixArtSigmaPipeline), avoiding per-candidate container packaging that SageMaker would require.
- SageMaker endpoint packaging effort per candidate would consume Cost_Cap budget without improving measurement quality; Cold_Start_Time for SageMaker-specific modes is measured separately (see below) only for the shortlisted models.

Instance selection by size class:

| Size class | Models | Instance | Rationale |
|---|---|---|---|
| Small (~0.6B) | PixArt-alpha, PixArt-Sigma | g5.xlarge or g6.xlarge (24 GB) | fits comfortably in bf16 |
| Medium (~12B) | FLUX.1-dev, FLUX.1-schnell, FLUX.1-Fill-dev | g6e.xlarge (L40S, 48 GB) | ~24 GB weights bf16 + activations |
| Large | FLUX.2, HunyuanImage | g6e.2xlarge / g6e.4xlarge, fall back to p4d slice only if required | sized after Evaluation_Matrix pins parameter counts |

On-demand instances (not spot) for benchmark determinism — a spot interruption mid-run wastes more budget than the on-demand premium costs at this scale. The harness records instance launch/terminate timestamps; cost = wall-clock instance-hours × on-demand rate, reconciled against actual billing (Cost Explorer, filtered by the benchmark tag) before the Cost_Model is finalized.

**Resource tagging discipline (Req 9.3):** every provisioned resource (instances, EBS volumes, the one S3 bucket for outputs, any temporary IAM role/security group) carries the tag `exploration=opensource-generation-models`. The teardown audit queries by this tag.

**Fixed test-case set (Req 2.2):**

- Inpainting cases (primary path): N source images drawn from representative industrial-inspection imagery, each with a binary mask PNG delimiting the defect region and a defect prompt resolved from the pipeline's `DEFAULT_PROMPT_TEMPLATE` shape (e.g., "scratch on a metal bracket"). At minimum 5 source/mask/prompt triples covering different defect types and mask sizes.
- Text-to-image cases: at minimum 3 defect prompts without source images.
- The same cases, byte-identical, run against every candidate. Seeds are fixed per test case so reruns are comparable.

**Metrics capture (Req 2.3, 2.6, 2.7):** the harness writes one `metrics.json` per run:

```json
{
  "run_id": "...", "model": "...", "instance_type": "...",
  "account": "164152369890", "region": "us-east-1",
  "model_load_seconds": 0.0,
  "cases": [
    {"case_id": "...", "task_type": "inpainting|text_to_image",
     "latency_seconds": 0.0, "seed": 0, "output_uri": "...",
     "status": "ok|failed", "failure_mode": null}
  ],
  "instance_hours": 0.0, "estimated_cost_usd": 0.0,
  "billing_reconciled_cost_usd": null
}
```

Quality assessment is human-scored per inpainting output on a fixed rubric (mask adherence: did the defect stay inside the mask; background preservation outside the mask; defect realism; prompt fidelity), each 1–5, recorded in `notes.md` alongside the outputs. Automated metrics (e.g., LPIPS outside the mask region vs source) are captured where cheap but the rubric is authoritative — the pipeline's consumer is a human approving previews.

Cold_Start_Time in Phase C means model-load-to-first-image on a fresh instance (captured as `model_load_seconds` + first-case latency). Hosting-option-specific cold starts (SageMaker async scale-from-zero, etc.) are estimated in the hosting comparison from AWS documentation and, only for the 1–2 shortlisted models and only if Cost_Cap headroom remains, measured with a short-lived SageMaker endpoint.

**Cost_Cap enforcement (Req 2.4, 2.9):**

- The protocol fixes the Cost_Cap (proposed: USD 500; final number set in benchmark-protocol.md before provisioning).
- The harness maintains a running ledger (`benchmark-results/README.md` cost table) updated at every instance launch and terminate. Before each provisioning action, `should_provision(spend_so_far + projected_run_cost, cap)` must be true; the check is a pure function in the harness with a unit test.
- Instances additionally carry a self-terminating guard (cron `shutdown` after a per-run wall-clock budget) so an abandoned instance cannot silently burn budget.
- If the ledger reaches the cap, remaining runs are recorded as `incomplete` in the README with the reason (Req 2.9).

**Teardown checklist and verification (Req 2.8, 9.3, 9.4):**

1. Terminate all EC2 instances tagged `exploration=opensource-generation-models`; verify `describe-instances` with the tag filter returns no non-terminated instances.
2. Delete unattached EBS volumes and snapshots with the tag.
3. Delete any SageMaker endpoints/endpoint-configs/models with the tag (if the shortlist measurement ran); verify list calls return empty.
4. Copy representative outputs into `benchmark-results/`, then delete the benchmark S3 bucket.
5. Delete temporary security groups / key pairs / IAM roles created for the benchmark.
6. Capture the audit evidence (CLI outputs of each verification query) into `teardown-audit.md`.
7. Portal-untouched verification: compare a pre-exploration snapshot of `aws cloudformation describe-stacks` (stack names + `LastUpdatedTime`) against a post-exploration snapshot; diff must be empty (Req 9.4). Also `git status` over `edge-cv-portal/` and `src/` must show no modifications (Req 9.2).

The pre-exploration stack snapshot is captured in Phase B, before any provisioning, and committed under `benchmark-results/`.

## Components and Interfaces

### Evaluation_Matrix (Requirement 1)

One row per Candidate_Model, columns fixed by the ACs:

| Column | Source AC | Vocabulary |
|---|---|---|
| Capability flags | 1.2 | `text_to_image`, `inpainting`, `image_variation`, `seed`, `cfg_scale` — booleans, same keys as `MODEL_CATALOG` |
| Inpainting path | 1.3 | `native` \| `official-variant` \| `community` \| `unsupported` |
| License | 1.4 | name, commercial-use terms, license text URL |
| Resources | 1.5 | parameter count, min/recommended GPU memory, satisfying AWS instance types |
| Weights access | 1.6 | location, `open` \| `gated` \| `api-only`, redistribution restrictions |
| Benchmark status | 1.7 | `included` \| `excluded (weights unobtainable)` with evidence link |

Known starting points to verify during Phase A (desk research confirms, the matrix records evidence):

- **FLUX.1-dev**: ~12B, non-commercial license → Legal_Review_Flag downstream; inpainting via **FLUX.1-Fill-dev** official variant; gated HF download.
- **FLUX.1-schnell**: ~12B, Apache 2.0; no official fill variant — inpainting path likely `community` or `unsupported`; open HF download.
- **FLUX.2**: newer BFL release; license, size, inpainting path, and weights access all to be verified — may be `api-only` for some variants, which would trigger 1.7 exclusion.
- **HunyuanImage**: Tencent license (community license with commercial clauses to record); large model; inpainting path to verify.
- **PixArt-alpha / PixArt-Sigma**: ~0.6B, openly licensed, open HF download; inpainting support weak — expect `community` or `unsupported`; they remain valuable as cheap text-to-image data points.

### Benchmark_Protocol (Requirement 2)

Sections, in order: candidate list (post-1.7 exclusions) → test-case set (frozen inputs, committed to `artifacts/benchmark-harness/cases/`) → per-run procedure (launch, load, run cases, capture metrics, terminate) → metrics definitions → quality rubric → Cost_Cap and ledger procedure → teardown checklist → evidence requirements. The protocol is committed before any provisioning task begins (Req 2.1) — the tasks.md ordering enforces this gate.

### Hosting Comparison (Requirement 3)

Comparison table over the five Hosting_Options × the evaluation dimensions:

| Dimension | Method |
|---|---|
| Always-on support | documented capability review |
| On-demand / scale-to-zero | mechanism (SageMaker async scale-to-zero via `MinInstanceCount=0`; EC2 stop/start orchestration; ECS/EKS scale-to-zero with Karpenter/scaling policies; JumpStart = real-time endpoint semantics) + Cold_Start_Time characteristics |
| Instance types per size class | small (PixArt) / medium (FLUX ~12B) / large (HunyuanImage) mapping from the Evaluation_Matrix resource rows |
| Lambda integration | invocation API (`sagemaker-runtime:InvokeEndpoint` / `InvokeEndpointAsync`, HTTP behind ALB/API GW for EC2/ECS), auth (SigV4 vs token), timeout analysis: portal generation Lambda budget vs measured per-image latency; async pattern required where latency exceeds synchronous budget |
| Ranking | per Availability_Mode, with rationale citing the above (Req 3.6) |

**GPU quota audit (Req 3.4):** a read-only script (`benchmark-harness/quota_audit.py`) calls `service-quotas` (`ListServiceQuotas` for EC2 running-on-demand G/P/VT instance vCPU quotas, and SageMaker per-instance-type endpoint quotas) and writes the current values + needed increases into the hosting comparison. Read-only; not Benchmark_Infrastructure.

### Cost_Model (Requirement 4)

Structure:

- **Usage_Profiles (≥3, Req 4.2):** `dev-light` (~50 images/day, concurrency 1, business hours), `steady-team` (~500 images/day, concurrency 2), `production-sustained` (~5,000 images/day, concurrency 4, 24×7). Exact numbers finalized in the artifact.
- **Estimate grid (Req 4.1, 4.3):** rows = (Candidate_Model that passed benchmarks) × (viable Hosting_Option) × (Availability_Mode); columns = the Usage_Profiles. Always-on cost = instance-hours × 730 × rate; on-demand cost = images × measured latency × rate + cold-start overhead amortization, using current us-east-1 pricing and measured Benchmark_Run latency.
- **Baseline (Req 4.4):** Nova Canvas Bedrock per-image price × profile volume, one baseline row per profile.
- **Cold-start tradeoff (Req 4.5):** for each on-demand combination, a stated tradeoff line: monthly savings vs always-on against the Cold_Start_Time the first request after idle pays.

Implemented as a markdown document with the arithmetic in embedded tables; if a companion spreadsheet-style script is written, its cost function gets the monotonicity sanity check (more images never lowers on-demand cost).

### Model_Registry_Proposal (Requirement 5)

Design-document skeleton the proposal must fill:

- **Schema (5.2):** a DynamoDB-backed table (consistent with the portal's existing Lambda+DynamoDB backend) whose item shape is a superset of a `MODEL_CATALOG` entry: `model_id`, `display_name`, `capabilities` (same five flags), `max_images_per_call`, `randomization_defaults`, plus `provider_type` (`bedrock` | `selfhosted`), `endpoint_config`, `availability_mode`, `enabled`. A field-coverage table in the proposal maps every existing `MODEL_CATALOG` key to a schema attribute (mechanically checkable against `synthetic_core.py`).
- **Per-environment config (5.3):** `endpoint_config` keyed by environment (`prod`, `dev`) so one entry carries different endpoints/Availability_Modes per environment.
- **Admin UI operations (5.4):** add / edit / enable / disable, with the API surface sketched (routes, request shapes) — proposal only, no implementation.
- **Migration path (5.5):** seed the registry from `MODEL_CATALOG` at deploy; read path prefers registry with catalog fallback behind a flag; Bedrock entries keep working throughout.
- **Availability filtering generalization (5.6):** `filter_available_models` generalizes to: Bedrock entries → existing `ListFoundationModels` intersection; selfhosted entries → endpoint health/reachability check (describe-endpoint status or HTTP health probe), with a cached result to keep `GET /synthetic/models` fast.
- **Authorization (5.7):** registry writes restricted to the portal admin role via the existing authorizer; the proposal names the concrete mechanism after reviewing the portal's current RBAC.

### Integration_Proposal (Requirement 6)

Skeleton:

- **Selfhosted_Provider (6.2):** generalizes the stability-generation-models Provider/Request_Adapter split: adapter selection moves from model-id prefix to the registry's `provider_type`; the selfhosted adapter invokes `sagemaker-runtime:InvokeEndpoint(Async)` or a plain HTTPS endpoint instead of `bedrock:InvokeModel`.
- **Per-model adapter mappings (6.3):** for each recommended model, a table mapping generation-task inputs (source image, mask, resolved prompt, Task_Seed, randomization params) → endpoint request schema, and response → image bytes in the form the generation worker consumes today.
- **Pipeline_Invariants (6.4):** explicit subsection per invariant: `derive_task_seed` untouched (seed flows into the endpoint request's seed field, modulo any per-model seed-domain note); per-preview metadata (model id, seed, resolved prompt) recorded in the same record shape; Mask_Region recorded for inpainting tasks so `bbox_from_mask` auto-annotation works unchanged; Nova Canvas request path byte-identical (the bedrock adapter is not modified).
- **Error taxonomy (6.5):** `endpoint_unreachable`, `endpoint_cold_starting`, `generation_failure`, `malformed_response` → each mapped to the existing per-task failure recording (failure reason on the Preview record, plan continues).
- **Timeout handling (6.6):** measured latencies vs the portal Lambda's synchronous budget; where exceeded (expected for FLUX-class on-demand cold starts), the proposal specifies SageMaker async inference (S3-out + notification) or a polling task-state pattern consistent with the existing generation-plan task model.

### Decision_Record (Requirements 7, 8)

ADR format: context → decision(s) → alternatives considered with rejection reasons (8.3) → consequences → open questions/prerequisites (8.4: quota increases, unresolved Legal_Review_Flags). Contains the licensing disposition table (7.1: `cleared` | `legal-review-required` | `unsuitable` per model; 7.2/7.3: Legal_Review_Flag with specific clauses and intended usage for FLUX.1-dev and any other restrictive license; 7.4: flagged models excluded from the production-recommended set while keeping their benchmark data). If no candidate meets the inpainting bar, the record states it and recommends a fallback direction (8.5) — e.g., stay on Nova Canvas + revisit the on-hold stability spec.

## Data Models

The exploration's "data models" are the artifact schemas already defined above: the Evaluation_Matrix row schema, the `metrics.json` run record, the cost ledger table, and the registry item shape proposed (not implemented) in the Model_Registry_Proposal. No portal data models change.

## Error Handling

- **Benchmark case failure (Req 2.10):** the harness records `status: failed` + `failure_mode` for the case and continues with remaining cases; a model that cannot produce usable inpainting output is still run through text-to-image cases.
- **Cost_Cap breach (Req 2.9):** provisioning stops; incomplete runs recorded; downstream phases proceed on partial data with the gap noted in the Decision_Record.
- **Weights unobtainable (Req 1.7):** recorded with evidence in the matrix; model excluded from Phase C; still receives a licensing disposition row (from desk research) in the Decision_Record.
- **Teardown verification failure:** if any tagged resource survives the checklist, teardown is not "confirmed" — the audit re-runs until the tag-filtered queries return empty; only then is `teardown-audit.md` finalized.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

This is a planning spec: the deliverables are documents and benchmark evidence, so most verification is checklist review and audited operational evidence rather than property-based testing. The properties below are the pragmatic invariants the exploration enforces; the small harness functions that are pure code get lightweight automated checks.

### Property 1: Deliverable completeness over the candidate set

For every Candidate_Model (FLUX.1-dev, FLUX.1-schnell, FLUX.2, HunyuanImage, PixArt-alpha, PixArt-Sigma), the Evaluation_Matrix contains a row with all required fields (capability flags in MODEL_CATALOG vocabulary, inpainting-path classification from the closed vocabulary, license name/terms/URL, parameter count and GPU memory and instance types, weights location and access mechanism and restrictions), and the Decision_Record contains a licensing disposition for that model. Verified by review checklist.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 7.1**

### Property 2: Exclusion consistency

For any Candidate_Model marked excluded in the Evaluation_Matrix (weights unobtainable), no Benchmark_Run results exist for that model; and for any Candidate_Model carrying an unresolved Legal_Review_Flag in the Decision_Record, that model does not appear in the production-recommended set (its benchmark results are retained). Verified by cross-document review.

**Validates: Requirements 1.7, 7.4**

### Property 3: Cost_Cap invariant

For every provisioning action taken during Phase C, the ledger spend recorded before that action plus the projected run cost is below the Cost_Cap; equivalently, the harness's `should_provision(spend, projected, cap)` pure function returns false whenever `spend + projected >= cap`. The pure function is unit-tested across below/at/above boundary values; the ledger is reviewed against the reconciled billing total.

**Validates: Requirements 2.4, 2.9**

### Property 4: Benchmark result-record completeness

For every completed Benchmark_Run, the run's `metrics.json` contains, for every test case attempted, a latency, seed, status, and (on success) output reference; and the run record contains instance-hours and estimated cost; and for every inpainting-capable model, quality-rubric scores exist for every inpainting case. Enforced by a harness schema assertion before a run is marked complete.

**Validates: Requirements 2.6, 2.7**

### Property 5: Failure isolation in benchmark runs

For any test case that fails during a Benchmark_Run, the harness records the failure mode for that case and still executes every remaining test case in the run. Verified by a harness unit test injecting a failing case.

**Validates: Requirements 2.10**

### Property 6: Teardown completeness

After teardown, for every AWS resource type the harness can create (EC2 instances, EBS volumes/snapshots, S3 bucket, SageMaker endpoints/configs/models, security groups, IAM roles), the tag-filtered describe/list query for `exploration=opensource-generation-models` returns an empty set, and the evidence is archived in `teardown-audit.md`. Verified by the audited teardown script output.

**Validates: Requirements 2.8, 9.3**

### Property 7: Portal non-modification

At exploration close, the git working tree shows no modifications under `edge-cv-portal/` or `src/` attributable to the exploration (all new files live under `.kiro/specs/opensource-generation-models/`), and the pre- vs post-exploration CloudFormation stack snapshots (stack names and LastUpdatedTime) are identical. Verified by the archived snapshot diff and change-set review.

**Validates: Requirements 9.1, 9.2, 9.4**

### Property 8: Registry schema field coverage

For every field of every entry in the existing `MODEL_CATALOG` (`model_id`, `display_name`, the five capability flags, `max_images_per_call`, `randomization_defaults`), the Model_Registry_Proposal's schema defines an attribute that expresses it. Verified by the proposal's field-coverage table cross-checked against `synthetic_core.py`.

**Validates: Requirements 5.2**

### Property 9: Cost model coverage and monotonicity

For every combination of (Candidate_Model that passed Benchmark_Runs) × (viable Hosting_Option) × (Availability_Mode), the Cost_Model contains a monthly estimate for each of the ≥3 Usage_Profiles plus the Nova Canvas baseline; and where the estimates are script-generated, on-demand cost is monotonically non-decreasing in images-per-day. Verified by coverage cross-check; monotonicity spot-checked if scripted.

**Validates: Requirements 4.1, 4.2, 4.3, 4.4**

## Testing Strategy

Because deliverables are documents and audited operational evidence, the testing strategy is dominated by structured verification rather than automated test suites:

- **Review checklists** (Properties 1, 2, 8, 9): each deliverable task in tasks.md carries an explicit checklist derived from its ACs; the deliverable is not complete until every checklist item maps to a section/row.
- **Harness unit tests** (Properties 3, 5): the benchmark harness's pure functions (`should_provision`, the continue-on-failure case loop, the metrics-record schema assertion) get small pytest tests in `artifacts/benchmark-harness/tests/`. These are ordinary example-based tests — the input spaces are trivial and PBT would add no value.
- **Operational audits** (Properties 4, 6, 7): scripted evidence capture (metrics schema assertion at run completion, tag-filtered teardown queries, stack-snapshot diff, quota audit) with outputs archived under `benchmark-results/` so the Decision_Record's claims are traceable to evidence.

No property-based tests are planned: no deliverable is a pure function over a large input space, and the benchmark runs are expensive one-shot operations — the PBT decision guide classifies everything here as EXAMPLE, INTEGRATION, or SMOKE.
