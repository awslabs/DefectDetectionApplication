# ADR: Open-Source Image Generation Models for the Synthetic Defect Data Pipeline

**Task 9.1 deliverable** (_Requirements: 7.1, 7.2, 7.3, 7.4, 8.1, 8.2, 8.3, 8.4, 8.5_).
Status: **Accepted (exploration outcome)** · Date: 2026-08-17 ·
Portal_Account 164152369890, us-east-1 · Branch
`spec/opensource-generation-models-exploration`

Consolidates: `evaluation-matrix.md` (Req 1) · `benchmark-protocol.md` +
`benchmark-results/` (Req 2) · `hosting-comparison.md` (Req 3) ·
`cost-model.md` (Req 4) · `model-registry-proposal.md` (Req 5) ·
`integration-proposal.md` (Req 6) · `quota-audit.md` (Req 3.4).

---

## 1. Context

In-region Bedrock offers the synthetic defect pipeline very little: Titan Image
Generator is retired, Nova Canvas is **lifecycle `LEGACY`** in us-east-1 with end
of life **2026-09-30** and is not invokable on this account, and the working
inpainting model today is the Bedrock inference profile
`us.stability.stable-image-inpaint-v1:0`. The pipeline's make-or-break capability
is **mask-constrained inpainting**: insert a defect inside a mask, leave every
pixel outside it alone, and record the Mask_Region so `bbox_from_mask`
auto-annotation works.

Six candidates were desk-researched and **all six were benchmarked hands-on** on
tagged, torn-down GPU infrastructure for **USD 11.11 of the USD 500 Cost_Cap**
(2.2 %). Teardown is verified clean: every tag-filtered query empty, stack
snapshot diff empty, `git status` over `edge-cv-portal/` and `src/` clean
(`benchmark-results/teardown-audit.md`).

## 2. The decisive finding (Req 8.5)

> **Exactly one benchmarked model clears the inpainting bar with true mask
> parity — FLUX.1-Fill-dev — and it is licensed non-commercial. Under Req 7.4 an
> unresolved Legal_Review_Flag excludes a model from the production-recommended
> set. Therefore: no candidate is both inpainting-capable and licence-clear for
> production today.**

Evidence:

| Model | Mask path | Outside-mask MAE (9 cases) | Mask adherence (rubric) | Verdict |
|---|---|---|---|---|
| **FLUX.1-Fill-dev** | official `FluxFillPipeline`, real binary mask | **1.0–6.1** | **4.7 / 5** | clears the bar |
| FLUX.1-schnell | community `FluxInpaintPipeline` (latent noising) | 0.8–4.5 far, **ring 2–3× far** | 3.9 / 5 | borderline — visible boundary seams, loose prompt control |
| FLUX.2 [dev] | **none** — no `mask_image` parameter exists; instruction-edit path | **9.6–67.2** | 1.7 / 5 | fails: re-renders the whole frame |
| HunyuanImage-2.1 (substitute for 3.0) | none | — | — | fails: `unsupported_task` on all 9 cases (text-to-image only) |
| PixArt-alpha | none | — | — | fails: `unsupported_task` on all 9 cases |
| PixArt-Sigma | none | — | — | fails: `unsupported_task` on all 9 cases |

**Fallback direction (Req 8.5): stay on Bedrock for the production primary path**
— specifically the live incumbent `us.stability.stable-image-inpaint-v1:0` — and
resume the on-hold `stability-generation-models` spec, which already carries the
Provider/Request_Adapter work this exploration generalized. Self-hosting is a
*second* source to add later, not a replacement to switch to now. Nova Canvas is
**not** a viable fallback (legacy, not invokable, EOL 2026-09-30), which makes
finishing the stability spec time-sensitive independently of this exploration.

## 3. Decisions (Req 8.1, 8.2)

### D1 — Production primary path stays on Bedrock

Keep `us.stability.stable-image-inpaint-v1:0` as the production inpainting model.
**Rationale:** the only self-hosted model with measured mask parity carries an
unresolved Legal_Review_Flag (Req 7.4 exclusion); the incumbent already works, has
no GPU quota or cold-start exposure, and costs $0.03–0.08/image against
$1,637–8,176/month for always-on self-hosting, which only breaks even above
≈41,000 images/month (`cost-model.md` §5).

### D2 — FLUX.1-schnell is the only production-eligible self-hosted candidate, and it is **conditional**

| Environment | Hosting_Option | Availability_Mode | Instance | Measured basis |
|---|---|---|---|---|
| prod | SageMaker real-time endpoint | always-on | ml.g6e.2xlarge | 20.3 s/image inpaint, fits the 60 s invocation ceiling with margin |
| dev | SageMaker async endpoint | on-demand (`MinCapacity=0`) | ml.g6e.2xlarge | est. 364 s cold start (est. provision + measured 4.2 s load), then 20.3 s/image |

**Rationale:** Apache 2.0 — the only benchmarked candidate cleared for commercial
self-hosting that has any inpainting story at all; fastest FLUX measured (1.8×
Fill-dev); $0.017–0.037/image on-demand, cheaper than every Bedrock baseline at
every Usage_Profile.
**Conditions before adoption (all three must pass):** (a) a human reviewer accepts
the measured quality gap — mask adherence 3.9/5, ring MAE 2–3× far, i.e. visible
seams, worst on real photographs; (b) the binary-mask input gap is closed
(`integration-proposal.md` §4); (c) real SageMaker scale-from-zero cold start is
measured, not estimated.

### D3 — FLUX.1-Fill-dev is approved for **non-production evaluation only**

| Environment | Hosting_Option | Availability_Mode | Instance |
|---|---|---|---|
| dev | SageMaker async endpoint | on-demand | ml.g6e.2xlarge |
| prod | **excluded** (Req 7.4) | — | — |

**Rationale:** it is the quality leader (mask adherence 4.7/5, background
preservation 4.4/5, outside-mask MAE 1.0–6.1) and the reference against which any
alternative is judged, so keeping a dev endpoint is valuable. Its licence forbids
production/commercial use of the *weights*, so it cannot serve prod.
**Caveat that legal review must settle:** even internal dev use inside a
commercial product's development is not self-evidently "non-commercial" under the
FLUX.1 [dev] licence. Until legal answers, D3 is *proposed*, not authorized —
treat it as pending, not granted.

### D4 — Benchmark FLUX.2 [klein] before any further FLUX.2 work

FLUX.2 [klein] (4B/9B, **Apache 2.0**) is the only FLUX.2 family member with a
real mask inpaint pipeline in diffusers (`Flux2KleinInpaintPipeline`) and was never
benchmarked (noted in `benchmark-results/flux.2/run-001/notes.md`). It is the only
identified candidate that could be **both** licence-clear **and** mask-capable —
i.e. the only path out of the §2 impasse without legal relief. One small run:
g6e.2xlarge, the same frozen cases, well inside the remaining Cost_Cap headroom.

### D5 — Hosting shape, if and when self-hosting proceeds

SageMaker real-time for always-on prod; **SageMaker async for on-demand dev** —
the only Hosting_Option that queues a request arriving at zero instances instead
of failing it, so the cold start is a latency event rather than an error path
(`hosting-comparison.md` §5). EC2 + inference server remains the documented
fallback (26 % cheaper per hour, shortest cold start ≈2.7 min, at the cost of
owning orchestration, TLS, token auth, and a VPC-attached Lambda).

### D6 — Registry and adapter design are adopted as the integration blueprint

`model-registry-proposal.md` (DynamoDB registry, strict superset of
`MODEL_CATALOG`, per-environment `endpoint_config`, PortalAdmin-only writes) and
`integration-proposal.md` (Selfhosted_Provider, adapter mappings, four-code error
taxonomy, async/resume pattern for the 15-minute worker) are the starting point
for the implementation spec. Both preserve every Pipeline_Invariant, including a
byte-identical Bedrock request path.

## 4. Licensing disposition (Req 7.1, 7.2, 7.3, 7.4)

Every Candidate_Model gets a disposition. Authoritative clause detail:
`evaluation-matrix.md`.

| Candidate_Model | Licence | Disposition | Legal_Review_Flag | In production-recommended set? | Benchmark results retained? |
|---|---|---|---|---|---|
| **FLUX.1-dev** | FLUX.1 [dev] Non-Commercial | **legal-review-required** | ✅ | **No** (Req 7.4) | ✅ `benchmark-results/flux.1-dev/run-001/` |
| **FLUX.1-Fill-dev** | FLUX.1 [dev] Non-Commercial (same terms) | **legal-review-required** | ✅ | **No** (Req 7.4) — dev-only per D3 | ✅ (same run dir; inpainting cases) |
| **FLUX.1-schnell** | Apache 2.0 | **cleared** | — | **Yes**, conditional (D2) | ✅ `benchmark-results/flux.1-schnell/run-001/` |
| **FLUX.2 [dev]** | FLUX [dev] Non-Commercial | **unsuitable** (non-commercial **and** rejected on technical grounds — no mask API) | ✅ recorded, but **legal review not requested**: pointless for a model already rejected technically | No | ✅ `benchmark-results/flux.2/run-001/` |
| **HunyuanImage** (matrix row 3.0; 2.1 benchmarked as substitute) | Tencent Hunyuan Community License | **unsuitable** | ✅ | No | ✅ `benchmark-results/hunyuanimage/run-001/` |
| **PixArt-alpha** | CreativeML Open RAIL++-M (weights); AGPL-3.0 (repo code) | **cleared** (with use restrictions) | — | No — capability, not licence: text-to-image only | ✅ `benchmark-results/pixart-alpha/run-001/` |
| **PixArt-Sigma** | CreativeML Open RAIL++-M (weights); AGPL-3.0 (repo code) | **cleared** (with use restrictions) | — | No — text-to-image only | ✅ `benchmark-results/pixart-sigma/run-001/` |
| *FLUX.2 [klein]* (not a spec Candidate_Model; follow-up per D4) | Apache 2.0 | **cleared** (desk research only) | — | Not yet — unbenchmarked | n/a |

### Legal_Review_Flags — clauses and the intended usage to assess (Req 7.3)

**LRF-1 · FLUX.1-dev and FLUX.1-Fill-dev — FLUX.1 [dev] Non-Commercial License**

- Clauses: the licence grants use of the **weights and inference code** for
  non-commercial, non-production purposes only; **outputs** may be used
  commercially per the licence/model card; derivatives and redistribution inherit
  the same non-commercial terms.
- Intended usage to assess: hosting the weights on a SageMaker endpoint inside the
  Portal_Account to generate synthetic training data for a commercial defect
  detection product. Two distinct questions: (i) is production hosting of the
  weights permitted? (expected answer: **no**); (ii) is internal **development**
  use inside a commercial product's development cycle "non-commercial"? (unclear —
  this is what gates D3). A third question if (i) is ever revisited: does the
  output-commercial-use permission extend to using outputs as **training data**
  for another model?
- Consequence while unresolved: excluded from the production-recommended set;
  benchmark results retained in full.

**LRF-2 · FLUX.2 [dev] — FLUX [dev] Non-Commercial License**

- Clauses: same non-commercial weights restriction as LRF-1.
- Intended usage: none — the model is rejected on technical grounds (no mask API;
  outside-mask MAE 9.6–67.2; 81.5 s/image, the worst latency-per-dollar
  benchmarked). Flag recorded for completeness; **no legal review is requested**.

**LRF-3 · HunyuanImage — Tencent Hunyuan Community License**

- Clauses: (a) **§5(b)-type restriction prohibiting use of outputs to improve
  other AI models** — this pipeline's entire purpose is generating training data to
  improve a defect detection model, so this is likely fatal on its face; (b)
  territory exclusion — the licence **does not apply in the EU, UK, or South
  Korea**, which constrains where the portal and its users may operate; (c) a
  separate-licence threshold for very-large-MAU products (>100M MAU).
- Intended usage to assess: generating synthetic training images used to train
  customer defect detection models, served from us-east-1 to a potentially
  international user base.
- Consequence: **unsuitable** independent of quality — and the benchmark showed it
  cannot do mask inpainting anyway (text-to-image only in 2.1; no documented mask
  API in 3.0). Excluded; benchmark results retained.

**Note on the RAIL++ "cleared" entries.** PixArt's CreativeML Open RAIL++-M permits
commercial use but carries **behavioural use restrictions** that propagate to
downstream users; the AGPL-3.0 on the upstream *training/inference repo* does not
encumber the weights or diffusers usage, but the implementation must not vendor
that repo's code into the portal. Both points are advisory, not review-blocking —
and moot for the primary path, since neither model can inpaint.

## 5. Alternatives considered and why not selected (Req 8.3)

| Alternative | Why not selected |
|---|---|
| **FLUX.1-Fill-dev in production** (the quality winner) | Non-commercial licence → unresolved Legal_Review_Flag → Req 7.4 exclusion. Technically it is the recommendation; legally it is unavailable |
| **FLUX.2 [dev]** | No mask API (`Flux2Pipeline` has no `mask_image`); instruction-edit path re-renders the frame (outside-mask MAE 9.6–67.2, worse outside the mask than inside on 3 of 9 cases) → `bbox_from_mask` cannot be driven from it. Also 81.5 s/image (2.2× Fill-dev), needs NF4 to fit a 48 GB L40S at all, exceeds the 60 s `InvokeEndpoint` ceiling, $0.10–0.17/image, and non-commercial |
| **HunyuanImage-3.0** | Never benchmarked: bf16 needs 3–4×80 GB (p4de/p5) which breaks the Cost_Cap for a single run; substituted with 2.1 per the matrix's sanctioned option (c). Licence §5(b) is likely fatal regardless |
| **HunyuanImage-2.1** (the substitute actually run) | Text-to-image only (`unsupported_task` on all 9 inpaint cases); large-class instance cost for small-class capability ($0.05–0.11/image); same licence problems |
| **PixArt-alpha / PixArt-Sigma** | Text-to-image only — cannot serve the primary path at any price, despite being the cheapest per image ($0.003–0.02) and the cheapest to host. Also the slowest cold start measured (`model_load_seconds` 376–554 s, T5-XXL encoder) |
| **SageMaker JumpStart** | No curated entry for any candidate; degrades to a hand-built real-time endpoint with extra indirection |
| **ECS/EKS GPU service** | The portal runs no GPU cluster today (Lambda + DynamoDB + CDK). Adds a cluster, a 15–25 GB image pipeline and node lifecycle; slowest estimated cold start (5–12 min) for no measured benefit |
| **EC2 + inference server for production** | Cheapest per hour and shortest cold start, but you own TLS, token auth, health checks, patching, stop/start orchestration, and the Lambda must join the VPC. Kept as the documented fallback (D5), not the recommendation |
| **Always-on self-hosting at current volumes** | Break-even against the Bedrock baseline is ≈41,000 images/month (EC2) / 51,100 (SageMaker). At `dev-light` (1,100/month) always-on self-hosting is ~37× the per-image cost of the API |
| **Provisioned Throughput for Nova Canvas** | $55–60.50/hour per model unit — an order of magnitude worse than any self-hosted shape, and the model is legacy/EOL |
| **Keep the static `MODEL_CATALOG`** | Every model addition needs a Python edit + Lambda deploy; cannot express per-environment endpoints or Availability_Modes, which self-hosting requires. Superseded by D6 |

## 6. Consequences

**Positive.** The primary-path question is settled with measured evidence instead of
vendor claims: mask parity is a real discriminator, and only one of six candidates
has it. The registry and adapter designs are reusable whichever model wins (and
would also clean up the retired-Titan entry still sitting in `MODEL_CATALOG`).
Total exploration spend was USD 11.11.

**Negative / accepted risks.**

- The pipeline stays dependent on Bedrock in production, and its current Bedrock
  incumbent's stablemate (Nova Canvas) is EOL 2026-09-30 — the
  `stability-generation-models` spec becomes the critical path.
- Every SageMaker cold-start number in this exploration is an **estimate**; task 4.4
  was skipped by user decision (**not** blocked by quota — see §7).
- All FLUX latencies were measured under `enable_model_cpu_offload`, so 36.7 s and
  20.3 s are **upper bounds**; a resident-weights deployment is faster and would
  improve the economics in self-hosting's favour.
- HunyuanImage-3.0 (the matrix's actual row) was never benchmarked; the 2.1
  substitution is recorded everywhere it matters.
- Rubric scores are proxy-anchored with spot inspection; a human reviewer should
  confirm defect realism and prompt fidelity before D2's condition (a) is signed
  off.

## 7. Open questions and prerequisites for the implementation spec (Req 8.4)

### 7a. GPU quota prerequisites

| # | Quota | Code | Current | Needed | Status |
|---|---|---|---|---|---|
| 1 | `ml.g6e.xlarge for endpoint usage` | L-B0729CB4 | **4** | ≥4 | **satisfied** — increase 1→4, request `5abacbc48f714600aab88fb3048a73300lWtOjBn`, filed 2026-08-17T21:47:21Z, **APPROVED** 2026-08-17T22:59:29Z. Production headroom; **not** required for the exploration |
| 2 | `ml.g6e.2xlarge for endpoint usage` | L-F8D7F460 | **4** | ≥4 | **satisfied** — increase 1→4, request `a5045b7c81134c3db082d723129b9dd5KbYC9Qgs`, filed 2026-08-17T21:47:28Z, **APPROVED** 2026-08-17T22:59:29Z. Same note |
| 3 | `ml.g6e.4xlarge for endpoint usage` | L-93531071 | 1 | 2–4 | **required before prod** if the resident-bf16 shape is chosen (blue/green needs 2 concurrent) |
| 4 | `ml.g6e.8xlarge for endpoint usage` | L-96A28D02 | 1 | 2 | conditional — only if the 256 GiB host-RAM Phase C shape is carried into production |
| 5 | `ml.p4d/p4de/p5 …24xlarge/48xlarge for endpoint usage` | L-09F79647 / L-456B4C5F / L-16AF71F1 | 0 | 1+ | **deferred** — only for bf16 FLUX.2 or HunyuanImage-3.0, neither recommended |
| 6 | EC2 Running On-Demand G/VT and P vCPUs | L-DB2E81BA / L-417A185B | 768 / 768 | no change | ample |

**Correction on the record:** an earlier note in `benchmark-results/README.md` and
`teardown-audit.md` said g6e SageMaker endpoint quotas were **0** and used that as
the reason task 4.4 was skipped. That was **wrong** — the values were 1 (now 4 for
xlarge/2xlarge), which would have sufficed for one short-lived measurement
endpoint. Task 4.4 was skipped by **user decision**. Both documents are corrected;
the live audit is `quota-audit.md`.

### 7b. Unresolved Legal_Review_Flags

- **LRF-1 (FLUX.1-dev / Fill-dev)** — blocks D3 (dev use) and any future
  production use. The highest-value question in this record: if legal clears
  internal non-production use, the reference-quality model becomes available for
  evaluation; if legal ever cleared production use, D1 would be revisited.
- **LRF-3 (HunyuanImage)** — no review requested; the model is `unsuitable`
  on the output-reuse clause alone.
- **LRF-2 (FLUX.2 [dev])** — no review requested; rejected technically.

### 7c. Technical prerequisites

1. **Binary-mask acquisition is a hard blocker** for any self-hosted inpainting.
   The pipeline has no mask today (Nova Canvas uses a text `maskPrompt`), so
   `mask_region` is never recorded and annotation falls back to `image_diff`.
   Recommended: user-drawn region in the session UI, rasterized server-side, with
   an uploaded-mask API escape hatch (`integration-proposal.md` §4).
2. **Measure real SageMaker scale-from-zero cold start** for a ~34 GB FLUX
   pipeline. Quota is available now (`ml.g6e.2xlarge` = 4); one short-lived tagged
   endpoint closes the largest unmeasured risk in `hosting-comparison.md` and
   `cost-model.md`.
3. **Benchmark FLUX.2 [klein]** per D4 — the only candidate that could be both
   Apache 2.0 and mask-capable.
4. **Human quality review** of the committed representative outputs for Fill-dev vs
   schnell, to settle D2 condition (a).
5. **Weights supply chain:** FLUX repos are ~55–58 GB and HF-gated (licence
   acceptance). Production needs an internal S3 mirror plus a documented
   acceptance trail; the exploration's temporary HF-token SSM parameter was deleted
   during teardown.
6. **Resolution parity:** Phase C measured inpainting at 768² while the portal's
   annotation defaults assume 1024². Confirm served resolution and the
   `image_size` recorded in manifest records.
7. **Confirm the incumbent's price:** `us.stability.stable-image-inpaint-v1:0` has
   no published per-image SKU in the us-east-1 Pricing API; the cost model uses
   Nova Canvas's $0.04 as the arithmetic baseline and flags the substitution
   (`cost-model.md` §8). Conclusions are stable across the $0.03–0.08 band.
8. **Registry/IAM trust boundary:** a free-text `endpoint_name` in a database is a
   privilege-escalation path — scope the worker's `sagemaker:InvokeEndpoint` to an
   endpoint-ARN prefix and store `https` targets as secret references, never raw
   URLs (`model-registry-proposal.md` §7).
9. **Billing reconciliation remains `pending-with-evidence`:** the `exploration` tag
   is not an activated cost allocation tag on this account and Cost Explorer had not
   ingested the run day. The ledger (USD 11.11) is the authoritative spend record.

## 8. Deliverable index

| Deliverable | Requirement | File |
|---|---|---|
| Evaluation_Matrix | 1 | `evaluation-matrix.md` |
| Benchmark_Protocol | 2.1–2.4 | `benchmark-protocol.md` |
| Benchmark results + cost ledger + teardown audit | 2.5–2.10, 9.3, 9.4 | `benchmark-results/` (README.md, teardown-audit.md, `<model>/run-001/`) |
| Hosting comparison | 3 | `hosting-comparison.md` (+ `quota-audit.md` for 3.4) |
| Cost_Model | 4 | `cost-model.md` (+ `benchmark-harness/cost_model.py`, tests) |
| Model_Registry_Proposal | 5 | `model-registry-proposal.md` |
| Integration_Proposal | 6 | `integration-proposal.md` |
| Decision_Record | 7, 8 | this document |

---

## 9. Cross-document consistency review (task 9.2)

Run 2026-08-17 across all seven deliverables. **Property 1: Deliverable
completeness over the candidate set** and **Property 2: Exclusion consistency**
(design.md). Every check below was verified against the files as committed, and the
benchmark-directory checks were run mechanically (`ls
benchmark-results/<model>/run-001/`).

_Requirements verified: 1.1, 1.7, 7.1, 7.4, 8.1_

### 9a. Property 1 — every Candidate_Model has a complete matrix row and a licensing disposition

| Check | FLUX.1-dev | FLUX.1-schnell | FLUX.2 [dev] | HunyuanImage | PixArt-alpha | PixArt-Sigma |
|---|---|---|---|---|---|---|
| Evaluation_Matrix row present (1.1) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 5 capability flags in `MODEL_CATALOG` vocabulary (1.2) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Inpainting path from the closed vocabulary (1.3) | ✅ | ✅ | ✅ (Phase C revision, §9c) | ✅ | ✅ | ✅ |
| Licence name + commercial terms + text URL (1.4) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Params + min/rec GPU memory + AWS instance types (1.5) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Weights location + access mechanism + redistribution (1.6) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Benchmark status recorded (1.7) | ✅ included | ✅ included | ✅ included | ✅ included (2.1 substitution) | ✅ included | ✅ included |
| **Licensing disposition in this record (7.1)** | ✅ legal-review-required | ✅ cleared | ✅ unsuitable | ✅ unsuitable | ✅ cleared | ✅ cleared |
| Hosting recommendation or explicit non-recommendation (8.2) | ✅ dev-only (D3) | ✅ prod + dev (D2) | ✅ rejected (§5) | ✅ rejected (§5) | ✅ rejected (§5) | ✅ rejected (§5) |
| Cost_Model coverage across all 3 Usage_Profiles (4.1–4.3) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

FLUX.1-Fill-dev is covered inside the FLUX.1-dev row and disposition (same weights
licence, same run directory) — it is a variant of a Candidate_Model, not a seventh
candidate. **Property 1: PASS.**

### 9b. Property 2 — exclusion consistency

1. **"Excluded ⇒ no benchmark results".** No Candidate_Model is marked
   `excluded (weights unobtainable)` in the Evaluation_Matrix (Req 1.7 was never
   triggered; the only api-only artifacts, FLUX.2 [pro]/[flex], are variants outside
   the pinned [dev] row). The implication holds vacuously, and consistently: all six
   models have benchmark results. Verified present:
   `flux.1-dev/run-001/`, `flux.1-schnell/run-001/`, `flux.2/run-001/`,
   `hunyuanimage/run-001/`, `pixart-alpha/run-001/`, `pixart-sigma/run-001/` — each
   with `config.json`, `metrics.json`, `notes.md`, `outputs/`. **PASS.**
2. **"Unresolved Legal_Review_Flag ⇒ absent from the production-recommended set".**
   Flagged: FLUX.1-dev/Fill-dev (LRF-1), FLUX.2 [dev] (LRF-2), HunyuanImage
   (LRF-3). The production-recommended set is exactly
   {`us.stability.stable-image-inpaint-v1:0` (Bedrock incumbent, D1),
   FLUX.1-schnell (Apache 2.0, conditional, D2)} — **no flagged model appears in
   it**. FLUX.1-Fill-dev is confined to a non-production environment in D3 and that
   permission is itself marked pending legal. **PASS.**
3. **"Flagged models retain their benchmark results".** All three flagged models
   keep complete run directories with metrics, notes, rubric scores where
   applicable, and representative outputs. Nothing was deleted or downgraded on
   account of a licence flag. **PASS.**
4. **Superseded-run consistency.** `pixart-alpha/pixart-alpha-r1/` and
   `pixart-sigma/pixart-sigma-r1/` carry `SUPERSEDED.md` and
   `billing_reconciliation.status = "superseded-not-attributed"`; the canonical set
   is one `run-001/` per model, and the ledger (USD 11.11) is not double-counted.
   **PASS.**

**Property 2: PASS.**

### 9c. Findings and corrections made during the review

1. **Evaluation_Matrix contains two Property-1 checklist tables whose
   inpainting-path values disagree** (a duplicate review section from Phase A). The
   first table records FLUX.2 `native (caveated)`, HunyuanImage
   `official-variant (caveated)`, PixArt `community`; the second records FLUX.2
   `community`, HunyuanImage and PixArt `unsupported`. The **summary matrix at the
   top is authoritative for Phase A**, and **Phase C supersedes both** for three
   rows. A reconciliation note has been added to `evaluation-matrix.md` recording
   the Phase-C-resolved values: FLUX.2 [dev] → **`unsupported`** (no `mask_image`
   parameter; instruction-edit path fails mask parity), HunyuanImage-2.1 →
   **`unsupported`** (text-to-image only), PixArt-alpha/Sigma → **`unsupported`**
   (confirmed `unsupported_task` on all 9 inpaint cases). FLUX.1-dev
   (`official-variant`) and FLUX.1-schnell (`community`) are confirmed unchanged by
   measurement.
2. **The g6e SageMaker quota claim was wrong in two documents** and is corrected in
   `benchmark-results/README.md`, `benchmark-results/teardown-audit.md`, and
   `benchmark-harness/quota-audit-trial.md` (marked superseded), with the live audit
   in `quota-audit.md`. Task 4.4's skip reason is now recorded accurately as a user
   decision. See §7a.
3. **Two quota increase requests moved from pending to APPROVED** between Phase D
   closing and the Phase E audit; recorded as satisfied prerequisites in §7a rather
   than as open items.
4. **HunyuanImage naming discipline holds:** every document that quotes
   HunyuanImage latency, cost, or capability figures attributes them to the
   benchmarked **2.1** substitute, and the matrix's **3.0** row is never given
   measured numbers. Verified in `hosting-comparison.md`, `cost-model.md`, and this
   record.
5. **No portal code was modified.** Confirmed by the Phase D `git status` evidence
   in `teardown-audit.md` and by this exploration's commits touching only
   `.kiro/specs/opensource-generation-models/` (Req 9.1, 9.2).

### 9d. Outcome

**Property 1: PASS · Property 2: PASS**, with the three documentation
inconsistencies in §9c corrected in place rather than carried forward. All seven
deliverables exist, cover all six Candidate_Models, and agree with each other on
capability, licensing, and recommendation status.
