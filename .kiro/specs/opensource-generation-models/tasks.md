# Implementation Plan: Open-Source Generation Models Exploration

## Overview

This is an exploration/planning spec. Tasks produce documents, benchmark evidence, and design proposals under `.kiro/specs/opensource-generation-models/artifacts/` — no production portal code or CDK changes. The plan follows the design's phase structure (A: desk research → B: protocol + harness + pre-exploration snapshot → C: benchmark execution on temporary tagged GPU infrastructure → D: teardown + audit → E: hosting comparison + cost model → F: registry and integration proposals → G: decision record).

**Gates enforced by task ordering and the dependency graph:**
- No provisioning task runs before `benchmark-protocol.md` exists and harness tests pass (Req 2.1).
- The pre-exploration CloudFormation stack snapshot is captured before any provisioning (Req 9.4).
- Teardown + audit completes before analysis phases close (Req 2.8, 9.3, 9.4).

**⚠️ COST WARNING:** Tasks in Phase C (task group 4) provision real AWS GPU instances in the Portal_Account (164152369890, us-east-1) and incur real spend. Every provisioning step must pass the `should_provision` Cost_Cap check and update the cost ledger in `artifacts/benchmark-results/README.md` before launch and after terminate. All resources carry the tag `exploration=opensource-generation-models`.

## Tasks

- [x] 1. Phase A — Candidate model evaluation matrix (desk research)
  - [x] 1.1 Create artifacts scaffold and evaluation matrix skeleton
    - Create `artifacts/` directory layout per the design (evaluation-matrix.md, benchmark-results/, benchmark-harness/)
    - Author `artifacts/evaluation-matrix.md` skeleton: one row per Candidate_Model (FLUX.1-dev, FLUX.1-schnell, FLUX.2, HunyuanImage, PixArt-alpha, PixArt-Sigma) with the fixed column schema — capability flags in `MODEL_CATALOG` vocabulary (text_to_image, inpainting, image_variation, seed, cfg_scale), inpainting path (native | official-variant | community | unsupported), license (name, commercial terms, URL), resources (parameter count, min/recommended GPU memory, satisfying AWS instance types), weights access (location, open | gated | api-only, redistribution restrictions), benchmark status (included | excluded)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

  - [x] 1.2 Research and fill the FLUX family rows
    - Verify and record FLUX.1-dev (~12B, non-commercial license, FLUX.1-Fill-dev official inpainting variant, gated HF download), FLUX.1-schnell (Apache 2.0, inpainting path likely community/unsupported, open download), and FLUX.2 (license, size, inpainting path, weights access all to verify — api-only variants trigger 1.7 exclusion)
    - Cite evidence URLs (license text, model cards) in each cell
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

  - [x] 1.3 Research and fill HunyuanImage and PixArt rows, resolve exclusions
    - Verify and record HunyuanImage (Tencent community license commercial clauses, size class, inpainting path) and PixArt-alpha / PixArt-Sigma (~0.6B, open licenses, open HF download, weak inpainting)
    - For any model whose weights are unobtainable for self-hosting, record the finding with evidence and mark benchmark status `excluded (weights unobtainable)`
    - Pin parameter counts so Phase C large-class instance sizing (g6e.2xlarge / g6e.4xlarge / p4d fallback) can be finalized
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

  - [x] 1.4 Evaluation matrix completeness review
    - Run the review checklist from Property 1: every Candidate_Model row has all required fields populated with evidence links; exclusion markings are consistent (Property 2 first half)
    - Record the checklist outcome at the bottom of `evaluation-matrix.md`
    - **Property 1: Deliverable completeness over the candidate set** (matrix portion)
    - _Requirements: 1.1, 1.7_

- [x] 2. Phase B — Benchmark protocol, harness, and pre-exploration snapshot
  - [x] 2.1 Author the benchmark protocol
    - Write `artifacts/benchmark-protocol.md` with sections in design order: candidate list (post-1.7 exclusions) → frozen test-case set (≥5 inpainting source/mask/prompt triples covering different defect types and mask sizes, ≥3 text-to-image defect prompts, fixed seeds per case) → per-run procedure (launch, load, run cases, capture metrics, terminate) → metrics definitions (per-image latency, model_load_seconds, Cold_Start_Time, actual cost) → human quality rubric (mask adherence, background preservation, defect realism, prompt fidelity, each 1–5) → Cost_Cap (final USD number, proposed 500) and ledger procedure → teardown checklist (7 steps from the design) → evidence requirements
    - This artifact is the gate: no Benchmark_Infrastructure may be provisioned before it is committed
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 2.2 Commit the frozen benchmark test-case inputs
    - Place source images, binary mask PNGs, and prompt/seed definitions under `artifacts/benchmark-harness/cases/` with a `cases.json` manifest (case_id, task_type, prompt, seed, image/mask paths)
    - Same byte-identical cases run against every candidate
    - _Requirements: 2.2_

  - [x] 2.3 Implement the benchmark harness core
    - Write plain Python scripts under `artifacts/benchmark-harness/` (never deployed as portal infrastructure): `should_provision(spend_so_far, projected_run_cost, cap)` pure function; ledger read/update helpers for the `benchmark-results/README.md` cost table; the per-run case loop that records `status: failed` + `failure_mode` for a failing case and continues with remaining cases; the `metrics.json` writer and schema assertion (run_id, model, instance_type, account, region, model_load_seconds, per-case latency/seed/status/output_uri/failure_mode, instance_hours, estimated_cost_usd, billing_reconciled_cost_usd) enforced before a run is marked complete; run driver using `diffusers` official pipelines (FluxPipeline, FluxFillPipeline, Hunyuan pipelines, PixArtAlphaPipeline / PixArtSigmaPipeline)
    - Include the self-terminating instance guard (cron `shutdown` after per-run wall-clock budget) in the launch user-data template
    - _Requirements: 2.3, 2.4, 2.9, 2.10_

  - [x] 2.4 Write pytest boundary tests for should_provision
    - Tests in `artifacts/benchmark-harness/tests/`: below cap (true), exactly at cap (false), above cap (false), zero spend, projected alone exceeding cap
    - **Property 3: Cost_Cap invariant**
    - **Validates: Requirements 2.4, 2.9**

  - [x] 2.5 Write pytest failure-isolation test for the case loop
    - Inject a failing case mid-run; assert the failure mode is recorded for that case and every remaining case still executes
    - **Property 5: Failure isolation in benchmark runs**
    - **Validates: Requirements 2.10**

  - [x] 2.6 Write pytest metrics schema assertion test
    - Assert a complete run record passes and records missing latency/seed/status/output reference, instance-hours, or estimated cost are rejected before a run can be marked complete
    - **Property 4: Benchmark result-record completeness**
    - **Validates: Requirements 2.6, 2.7**

  - [x] 2.7 Capture the pre-exploration stack snapshot
    - Run `aws cloudformation describe-stacks` (stack names + LastUpdatedTime) against the Portal_Account and commit the snapshot under `artifacts/benchmark-results/` before any provisioning
    - Create `artifacts/benchmark-results/README.md` with the empty cost ledger table (Cost_Cap header from the protocol) and run index
    - _Requirements: 9.4, 2.4_

  - [x] 2.8 Implement the read-only GPU quota audit script
    - Write `artifacts/benchmark-harness/quota_audit.py` calling `service-quotas` for EC2 running on-demand G/P/VT vCPU quotas and SageMaker per-instance-type endpoint quotas in us-east-1; output a table consumed later by the hosting comparison
    - Read-only; not Benchmark_Infrastructure
    - _Requirements: 3.4_

- [x] 3. Checkpoint — provisioning gate
  - Ensure all harness pytest tests pass, `benchmark-protocol.md` is committed, the pre-exploration snapshot exists, and the Cost_Cap ledger is initialized. Ask the user if questions arise. Do not proceed to Phase C otherwise.

- [ ] 4. Phase C — Benchmark execution ⚠️ REAL AWS GPU COST
  - [x] 4.1 Benchmark PixArt-alpha and PixArt-Sigma (small class)
    - ⚠️ Provisions a g5.xlarge or g6.xlarge on-demand instance tagged `exploration=opensource-generation-models`. Before launch: update the ledger and verify `should_provision(spend + projected, cap)` is true. After terminate: record instance-hours and estimated cost in the ledger
    - Run all frozen cases per model; write `benchmark-results/pixart-alpha/<run-id>/` and `benchmark-results/pixart-sigma/<run-id>/` (config.json, metrics.json, representative outputs, notes.md with rubric scores for any inpainting cases); record failure modes and continue on per-case failure
    - _Requirements: 2.5, 2.6, 2.7, 2.9, 2.10_

  - [x] 4.2 Benchmark FLUX.1-schnell, FLUX.1-dev, and FLUX.1-Fill-dev (medium class)
    - ⚠️ Provisions a g6e.xlarge (L40S 48 GB) on-demand instance, same tag, same ledger + `should_provision` gate before launch and ledger update after terminate
    - Run all frozen cases; inpainting via FLUX.1-Fill-dev per the matrix's inpainting-path finding; capture model_load_seconds + first-case latency as Cold_Start_Time proxy; write per-run artifacts and rubric scores in notes.md
    - _Requirements: 2.5, 2.6, 2.7, 2.9, 2.10_

  - [x] 4.3 Benchmark FLUX.2 and HunyuanImage (large class, if included)
    - ⚠️ Provisions g6e.2xlarge / g6e.4xlarge (p4d slice only if required), sized from the matrix's pinned parameter counts; same tag, ledger, and `should_provision` gate per launch
    - Skip any model the matrix excluded (weights unobtainable) and record the skip in the run index; if the ledger reaches the Cost_Cap, stop provisioning and record remaining runs as `incomplete` with the reason in `benchmark-results/README.md`
    - _Requirements: 1.7, 2.5, 2.6, 2.7, 2.9, 2.10_

  - [ ]* 4.4 Measure SageMaker cold start for the shortlist (only if Cost_Cap headroom remains) — SKIPPED (optional, confirmed by user 2026-08-17: no further provisioning). ⚠️ CORRECTION FOR TASK 7.1/7.2: the skip reason recorded in `benchmark-results/README.md` states g6e SageMaker endpoint quotas are 0. A live re-check on 2026-08-17 shows they are **1** for ml.g6e.xlarge/2xlarge/4xlarge/8xlarge/12xlarge/16xlarge (0 only for 24xlarge/48xlarge) — a single endpoint WAS permissible, so quota was not the true blocker. Task 7.1 must record the verified values and correct that note; 7.2 uses documented cold-start estimates regardless. Increase requests to 4 each: ml.g6e.xlarge `5abacbc48f714600aab88fb3048a73300lWtOjBn`, ml.g6e.2xlarge `a5045b7c81134c3db082d723129b9dd5KbYC9Qgs` — both **APPROVED** 2026-08-17T22:59:29Z (verified in task 7.1; live values now 4/4). Corrections landed in `artifacts/quota-audit.md` §2, `benchmark-results/README.md`, `benchmark-results/teardown-audit.md`.
    - ⚠️ Provisions a short-lived SageMaker endpoint (tagged) for the 1–2 shortlisted models to measure scale-from-zero Cold_Start_Time; same ledger + `should_provision` gate; delete endpoint/config/model immediately after measurement
    - Skip entirely if headroom is insufficient — hosting comparison then uses documented estimates only
    - _Requirements: 2.3, 2.9, 3.2_

- [x] 5. Phase D — Teardown and audit (gate: nothing proceeds until clean)
  - [x] 5.1 Execute the teardown checklist and capture evidence
    - Run all 7 protocol teardown steps: terminate tagged EC2 instances, delete tagged EBS volumes/snapshots, delete tagged SageMaker endpoints/configs/models, copy representative outputs into `benchmark-results/` then delete the benchmark S3 bucket, delete temporary security groups/key pairs/IAM roles
    - Capture the CLI output of every tag-filtered verification query (`exploration=opensource-generation-models` returns empty for each resource type) into `artifacts/benchmark-results/teardown-audit.md`; if any tagged resource survives, re-run until all queries return empty
    - **Property 6: Teardown completeness**
    - _Requirements: 2.8, 9.3_

  - [x] 5.2 Reconcile the cost ledger against billing
    - Pull Cost Explorer actuals filtered by the benchmark tag; fill `billing_reconciled_cost_usd` in each run's metrics.json and finalize the ledger totals and run index (including any `incomplete` runs and reasons) in `benchmark-results/README.md`
    - _Requirements: 2.7, 2.9_

  - [x] 5.3 Verify the portal is untouched
    - Capture the post-exploration `describe-stacks` snapshot, diff against the Phase B pre-exploration snapshot (must be empty), and run `git status` over `edge-cv-portal/` and `src/` (must show no modifications); archive both results in `teardown-audit.md`
    - **Property 7: Portal non-modification**
    - _Requirements: 9.1, 9.2, 9.4_

- [x] 6. Checkpoint — teardown gate
  - Ensure `teardown-audit.md` shows all tag-filtered queries empty, the stack snapshot diff is empty, and the ledger is reconciled. Ask the user if questions arise.

- [x] 7. Phase E — Hosting comparison and cost model
  - [x] 7.1 Run the quota audit and record GPU quotas
    - Execute `quota_audit.py`; record current Portal_Account GPU_Quota values per required instance type in us-east-1 and the quota increases a future implementation would need
    - _Requirements: 3.4_

  - [x] 7.2 Author the hosting comparison
    - Write `artifacts/hosting-comparison.md` covering all five Hosting_Options (SageMaker real-time, SageMaker async, SageMaker JumpStart, EC2 + inference server, ECS/EKS GPU): always-on and on-demand Availability_Mode support with scale-to-zero mechanism and Cold_Start_Time characteristics (measured where 4.4 ran, documented estimates otherwise); instance types per size class from the matrix; Lambda integration path (invocation API, auth, timeout analysis vs measured per-image latency); quota findings from 7.1; ranking per Availability_Mode with rationale
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [x] 7.3 Author the cost model
    - Write `artifacts/cost-model.md`: ≥3 Usage_Profiles (dev-light ~50 img/day c1, steady-team ~500 img/day c2, production-sustained ~5,000 img/day c4 — finalize numbers); estimate grid of (benchmarked Candidate_Model) × (viable Hosting_Option) × (Availability_Mode) × Usage_Profile using measured latencies and current us-east-1 pricing; Nova Canvas Bedrock per-image baseline row per profile; cold-start cost-vs-latency tradeoff line per on-demand combination; if a companion cost script is written, include the monotonicity sanity check (more images never lowers on-demand cost)
    - **Property 9: Cost model coverage and monotonicity**
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

- [ ] 8. Phase F — Design proposals
  - [ ] 8.1 Author the model registry proposal
    - Write `artifacts/model-registry-proposal.md`: DynamoDB-backed schema superset of `MODEL_CATALOG` entries (model_id, display_name, five capability flags, max_images_per_call, randomization_defaults, provider_type, endpoint_config, availability_mode, enabled) with a field-coverage table cross-checked against `synthetic_core.py`; per-environment endpoint_config (prod/dev with distinct Availability_Modes); admin UI operations (add/edit/enable/disable) with sketched API surface; migration path (seed from catalog, registry-preferred read with catalog fallback flag, Bedrock entries functional throughout); availability filtering generalization (ListFoundationModels intersection for Bedrock entries, endpoint health/reachability with caching for selfhosted); authorization boundary restricting writes to the portal admin role via the existing authorizer
    - **Property 8: Registry schema field coverage**
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

  - [ ] 8.2 Author the integration proposal
    - Write `artifacts/integration-proposal.md`: Selfhosted_Provider generalizing the stability-generation-models Provider/Request_Adapter split with adapter selection by registry provider_type, invoking `sagemaker-runtime:InvokeEndpoint(Async)` or HTTPS instead of `bedrock:InvokeModel`; per-recommended-model adapter mapping tables (source image, mask, resolved prompt, Task_Seed, randomization params → request schema; response → image bytes); explicit Pipeline_Invariants subsections (unchanged `derive_task_seed`, per-preview model id/seed/resolved prompt recording, Mask_Region recording for `bbox_from_mask`, byte-identical Nova Canvas path); error taxonomy (endpoint_unreachable, endpoint_cold_starting, generation_failure, malformed_response) mapped to existing per-task failure recording; Lambda timeout analysis with the async invocation pattern for on-demand Availability_Mode
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

- [ ] 9. Phase G — Decision record
  - [ ] 9.1 Author the decision record
    - Write `artifacts/decision-record.md` in ADR format consolidating all prior artifacts: recommended Candidate_Models with Hosting_Option and Availability_Mode per model per environment and rationale citing benchmark evidence; alternatives considered with rejection reasons; licensing disposition table for every Candidate_Model (cleared | legal-review-required | unsuitable) with Legal_Review_Flags recording specific license clauses and intended production usage (FLUX.1-dev non-commercial terms at minimum); flagged models excluded from the production-recommended set while retaining benchmark results; open questions and prerequisites (GPU quota increases, unresolved Legal_Review_Flags); if no candidate met the inpainting bar, state the finding and recommend a fallback direction (e.g., Nova Canvas + revisit the stability spec)
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 8.1, 8.2, 8.3, 8.4, 8.5_

  - [ ] 9.2 Cross-document consistency review
    - Run the Property 1 and Property 2 checklists across artifacts: every Candidate_Model has a complete matrix row and a licensing disposition; excluded models have no benchmark results; legal-flagged models are absent from the production-recommended set but retain benchmark data; record the review outcome in `decision-record.md`
    - **Property 1: Deliverable completeness over the candidate set** / **Property 2: Exclusion consistency**
    - _Requirements: 1.1, 1.7, 7.1, 7.4, 8.1_

- [ ] 10. Final checkpoint
  - Ensure all harness tests pass, all seven deliverables exist under `artifacts/`, and the teardown audit is clean. Ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional (4.4 is skippable if Cost_Cap headroom is insufficient)
- Harness pytest tests (2.4–2.6) are required, not optional: the design mandates them as the Cost_Cap and result-integrity gates before real spend occurs
- ⚠️ Tasks 4.1–4.4 provision real AWS GPU resources with real cost; each explicitly updates the ledger and passes the `should_provision` gate before launch
- All artifacts land under `.kiro/specs/opensource-generation-models/artifacts/`; no portal source, frontend, or CDK code is modified (Req 9.2)
- Checkpoints 3 and 6 are hard gates: no provisioning before the protocol/tests/snapshot exist; no analysis close before teardown is verified clean

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "2.3", "2.7"] },
    { "id": 1, "tasks": ["1.2", "2.2", "2.4", "2.8"] },
    { "id": 2, "tasks": ["1.3", "2.5"] },
    { "id": 3, "tasks": ["1.4", "2.6"] },
    { "id": 4, "tasks": ["4.1"] },
    { "id": 5, "tasks": ["4.2"] },
    { "id": 6, "tasks": ["4.3"] },
    { "id": 7, "tasks": ["4.4"] },
    { "id": 8, "tasks": ["5.1"] },
    { "id": 9, "tasks": ["5.2"] },
    { "id": 10, "tasks": ["5.3"] },
    { "id": 11, "tasks": ["7.1"] },
    { "id": 12, "tasks": ["7.2", "8.1"] },
    { "id": 13, "tasks": ["7.3", "8.2"] },
    { "id": 14, "tasks": ["9.1"] },
    { "id": 15, "tasks": ["9.2"] }
  ]
}
```
