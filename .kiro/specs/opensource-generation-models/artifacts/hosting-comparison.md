# Hosting Comparison — Self-Hosted Generation Models on AWS

**Task 7.2 deliverable** (_Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_).
Portal_Account 164152369890, us-east-1. Written 2026-08-17 after Phase C/D closed.

Inputs: measured Phase C data (`benchmark-results/*/run-001/{metrics.json,notes.md}`),
the Evaluation_Matrix (`evaluation-matrix.md`), the live quota audit
(`quota-audit.md`), and the portal's current generation path
(`edge-cv-portal/backend/functions/synthetic_data.py`,
`edge-cv-portal/infrastructure/lib/synthetic-data-stack.ts` — read only, nothing
modified).

## 0. Measurement basis and what is estimated

Everything in the "measured" column below comes from Phase C runs on tagged EC2
GPU instances. **No SageMaker endpoint was created** (optional task 4.4 was
skipped by user decision on 2026-08-17 — *not* for quota reasons; see
`quota-audit.md` §2), so every SageMaker-specific scale-from-zero figure in this
document is an **ESTIMATE derived from AWS documentation plus the measured Phase C
model-load proxy**, and is labelled as such. No estimate is presented as a
measurement.

Measured per-image latency and model-load time (protocol §3; `model_load_seconds`
+ first-case latency is the Phase C Cold_Start_Time proxy):

| Model | Instance (Phase C) | Task | Measured latency/image | model_load_seconds (warm HF cache) | Weights download (cold) |
|---|---|---|---|---|---|
| PixArt-alpha | g5.xlarge | t2i 1024², 20 steps | 6.4–8.7 s | 375.8 s (fp32 T5 on 16 GiB host + swap) | 817 s first load |
| PixArt-Sigma | g5.xlarge | t2i 1024², 20 steps | 6.8–7.1 s | 554.0 s (incl. download) | — |
| FLUX.1-schnell | g6e.8xlarge | inpaint 768², 4 steps | 20.1–26.4 s (median 20.3) | 4.18 s | ≈450 s |
| FLUX.1-schnell | g6e.8xlarge | t2i 1024², 4 steps | 20.2–22.6 s | 4.18 s | ≈450 s |
| FLUX.1-dev + **Fill-dev** | g6e.8xlarge | inpaint 768², 50 steps | 36.4–40.4 s (median 36.7) | 4.44 s | ≈450 s/model |
| FLUX.1-dev | g6e.8xlarge | t2i 1024², 28 steps | 34.5–37.0 s | 4.44 s | ≈450 s |
| FLUX.2 [dev] (NF4) | g6e.8xlarge | instruction edit 768², 28 steps | 80.2–84.7 s | 38.1 s | 124 s |
| HunyuanImage-2.1 (NF4) | g6e.8xlarge | t2i 1024², 50 steps | 39.5–40.0 s | 18.9 s | 63 s |

Three measured facts shape every hosting recommendation:

1. **Only FLUX.1-Fill-dev cleared the inpainting bar with true mask parity**
   (outside-mask MAE 1.0–6.1 vs 9.6–67.2 for FLUX.2 [dev]'s instruction-edit
   path). FLUX.2 [dev] exposes **no mask API**; PixArt-alpha/Sigma and
   HunyuanImage-2.1 recorded `unsupported_task` on all 9 inpaint cases.
2. **All FLUX latencies above were measured under `enable_model_cpu_offload`.**
   37 s (Fill-dev) is the **offload-mode upper bound**, not a floor — a
   resident-weights deployment should be materially faster. Cost figures built on
   37 s are therefore conservative.
3. **Host RAM, not VRAM, was the binding constraint for the FLUX class.** Budget
   **≥64 GiB host RAM per resident FLUX pipeline** (Phase C used g6e.8xlarge with
   256 GiB; g6e.xlarge's 32 GiB could not hold a ~34 GB bf16 pipeline under
   offload). PixArt needs ≥32 GiB host RAM for its T5-XXL encoder.

## 1. Hosting_Option × Availability_Mode support (Req 3.1, 3.2)

| Hosting_Option | Always-on | On-demand (scale-to-zero) | Scale-to-zero mechanism | Cold_Start_Time character |
|---|---|---|---|---|
| **SageMaker real-time endpoint** | ✅ native | ✅ since the Dec-2024 *scale down to zero* feature (inference components + application auto scaling `MinInstanceCount`/`MinCapacity` = 0) | Application Auto Scaling target tracking to 0 copies; a request while at zero fails unless the caller pre-warms via `UpdateInferenceComponentRuntimeConfig` and polls | **ESTIMATE 4–10 min**: instance provision + container pull + weights fetch (≈34 GB FLUX bf16 from S3) + measured 4.4 s load. Not request-transparent — the caller must handle the "scaling up" state |
| **SageMaker async inference** | ✅ (fixed instance count) | ✅ **native and request-transparent** — the only option where a request arriving at zero instances is *queued* rather than failed | Application Auto Scaling on `ApproximateBacklogSizePerInstance`, `MinCapacity=0` | **ESTIMATE 4–10 min** for the first queued request (same provisioning path); subsequent requests in the same warm window pay only the measured per-image latency |
| **SageMaker JumpStart** | ✅ (deploys a real-time endpoint) | ⚠️ inherits real-time semantics only | same as real-time | same as real-time. **Not applicable to this shortlist**: JumpStart has no curated FLUX.1-Fill-dev/FLUX.2 entry, so it degrades to "real-time endpoint with a hand-built container" |
| **EC2 GPU + inference server** (diffusers/ComfyUI behind HTTPS) | ✅ (what Phase C measured) | ⚠️ only via **self-built** stop/start orchestration (EventBridge + Lambda `StartInstances` + readiness poll) | none managed; you own the state machine, the idle detector, and the readiness probe | **ESTIMATE 2–4 min** from `stopped` (EBS-resident weights, ~60–90 s boot + service start + measured 4.4 s model load, page cache cold). Fastest cold start of all options because weights never leave the volume |
| **ECS/EKS GPU service** | ✅ | ✅ via Karpenter/ASG scale-to-zero + KEDA/HPA on queue depth | cluster autoscaler removes the GPU node; task/pod count → 0 | **ESTIMATE 5–12 min**: node provision + a 15–25 GB CUDA/diffusers image pull + weights fetch (unless baked into an AMI/EBS snapshot or cached on EFS) |

Notes on the estimates: they are anchored on documented behaviour — SageMaker
async [can scale to zero and queues requests received at zero instances](https://docs.aws.amazon.com/sagemaker/latest/dg/async-inference-autoscale.html),
real-time endpoints [support scale-in to zero instances](https://docs.aws.amazon.com/sagemaker/latest/dg/endpoint-auto-scaling-zero-instances.html),
and container-image pull is a documented multi-minute component of scale-out that
SageMaker's [image caching](https://aws.amazon.com/about-aws/whats-new/2026/06/sagemakerai-inf-scale-out-time/)
reduces. The model-load term (4.2–4.4 s FLUX, 18.9 s Hunyuan, 38.1 s FLUX.2, 376–554 s
PixArt) is **measured**; the provisioning and image/weights-transfer terms are
**not**. Content from cited AWS pages was rephrased for compliance with licensing
restrictions.

**PixArt caveat for on-demand modes:** PixArt's measured `model_load_seconds` of
376–554 s is dominated by loading the fp32 T5-XXL encoder through swap on a
16 GiB host. On a ≥32 GiB host with fp16 encoder loading this collapses, but it
was not re-measured — treat PixArt's on-demand cold start as **unquantified**
rather than "fast because the model is small".

## 2. Instance types per size class (Req 3.3)

Size classes from the Evaluation_Matrix; instance choices corrected by the Phase C
host-RAM finding. EC2 hourly rates and SageMaker hosting rates are live
`aws pricing` values for us-east-1 (see `cost-model.md` §1 for the full table).

| Size class | Models | Serving shape | EC2 instance | SageMaker instance | Why |
|---|---|---|---|---|---|
| Small (~0.6B DiT + T5-XXL) | PixArt-alpha, PixArt-Sigma | resident bf16 | **g5.2xlarge** (24 GB VRAM, 32 GiB RAM) or g6.2xlarge | ml.g5.2xlarge / ml.g6.2xlarge | 24 GB VRAM is ample; the ≥32 GiB host-RAM step is required by the measured swap failure on g5.xlarge |
| Medium (~12B) | FLUX.1-schnell, FLUX.1-dev, **FLUX.1-Fill-dev** | quantized or cpu_offload | **g6e.2xlarge** (48 GB VRAM, 64 GiB RAM) | ml.g6e.2xlarge | minimum shape that satisfies "≥64 GiB host RAM per resident FLUX pipeline" |
| Medium (fastest) | same | resident bf16, no offload | **g6e.4xlarge** (128 GiB RAM) or g6e.8xlarge (256 GiB, Phase C box) | ml.g6e.4xlarge / ml.g6e.8xlarge | removes the offload penalty behind the 37 s upper bound; g6e.8xlarge is the only shape actually measured |
| Large (32B) | FLUX.2 [dev] | NF4 4-bit + offload | g6e.8xlarge | ml.g6e.8xlarge | measured working shape (bf16 + offload OOMed the 48 GB L40S) |
| Large (32B), production bf16 | FLUX.2 [dev] | resident bf16, ≥96 GB VRAM | g6e.12xlarge (4×L40S) or p4d.24xlarge | ml.g6e.12xlarge / ml.p4d.24xlarge | the realistic production shape if FLUX.2 [dev] were ever recommended (it is not) |
| Extra-large (80B MoE) | HunyuanImage-3.0 (never benchmarked) | bf16 multi-GPU | p4de.24xlarge / p5.48xlarge | ml.p4de.24xlarge / ml.p5.48xlarge | matrix sizing; substituted by 2.1 in Phase C |
| Large (17B) | HunyuanImage-2.1 (substitute actually run) | NF4 + offload | g6e.8xlarge | ml.g6e.8xlarge | measured working shape |

## 3. GPU quota position (Req 3.4)

Full transcript and the needed-increase list are in `quota-audit.md`. Summary:

- **EC2 is not a constraint.** Running On-Demand G and VT vCPUs = **768**
  (L-DB2E81BA), P vCPUs = **768** (L-417A185B) — 24 concurrent g6e.8xlarge or 8
  concurrent p4d.24xlarge.
- **SageMaker endpoint usage quotas** (per instance type, shared by real-time and
  async): `ml.g6e.xlarge` = **4**, `ml.g6e.2xlarge` = **4** (both raised from 1 by
  increases approved 2026-08-17T22:59:29Z), `ml.g6e.4xlarge` / `8xlarge` /
  `12xlarge` / `16xlarge` = **1**, `ml.g6e.24xlarge` / `48xlarge` = 0,
  `ml.p4d.24xlarge` / `p4de.24xlarge` / `p5.48xlarge` = **0**,
  `ml.g5.xlarge` = 4, `ml.g5.2xlarge` = 2, `ml.g6.2xlarge` = 1.
- **Correction to an earlier record:** the g6e SageMaker quotas were *never* 0.
  The earlier "quotas are 0" note (Phase B trial capture, repeated in the Phase C/D
  README and teardown audit) was wrong and has been corrected in place.
- **Increase a future implementation still needs:** `ml.g6e.4xlarge for endpoint
  usage` 1 → 2–4, so a resident-bf16 medium-class endpoint can be blue/green
  updated. p4d/p4de/p5 endpoint quotas (all 0) are only needed for bf16 FLUX.2 or
  HunyuanImage-3.0 — neither is recommended, so that is a deferred prerequisite.

## 4. Lambda integration path (Req 3.5)

**Current shape (verified, unchanged by this exploration).** `synthetic_data.py`
is one Lambda serving both the `/api/v1/synthetic` routes and the generation
worker. `POST .../generate` validates, persists the plan, self-invokes with
`InvocationType='Event'` (`internal_action: generation_worker`) and returns
**202**. The worker loops the plan **one `bedrock:invoke_model` call per task**,
writing each preview as it completes, with a `generation_pass`-conditional status
update so a stale worker cannot overwrite a newer pass. CDK config:
`memorySize: 1024`, `timeout: 15 min`.

| Hosting_Option | Invocation API | Auth | Timeout analysis against measured latency |
|---|---|---|---|
| SageMaker real-time | `sagemaker-runtime:InvokeEndpoint` | SigV4 via the worker's execution role; `sagemaker:InvokeEndpoint` on the endpoint ARN | Endpoint hard limit **60 s per invocation**. Fill-dev's measured 36.7 s (offload) fits with ~23 s of headroom — but the 40.4 s first-case value and any resolution/step increase erode it. Viable, tight; **not viable** for FLUX.2 [dev] (80–85 s > 60 s) |
| SageMaker async | `sagemaker-runtime:InvokeEndpointAsync` (S3 in, S3 out, optional SNS) | SigV4, same role; plus S3 read/write on the payload/output prefixes | No 60 s ceiling (documented for processing times up to 60 min). Removes the per-call timeout question entirely; the worker submits and polls/consumes the S3 result |
| SageMaker JumpStart | same as real-time | same | same 60 s ceiling |
| EC2 + inference server | HTTPS to an internal ALB (or the instance in-VPC) | SigV4 not available → mTLS or a bearer token from Secrets Manager, security-group restricted to the Lambda's ENI subnets; Lambda must be VPC-attached | No managed per-request ceiling; ALB idle timeout is configurable (default 60 s, raise to ≥120 s). Fits every measured latency including FLUX.2's 85 s |
| ECS/EKS GPU | HTTPS via ALB/NLB (or API Gateway) | same as EC2 | same as EC2; API Gateway REST integration caps at 29 s by default, which **fails** for every FLUX shape → ALB, not API GW |

**Timeout budget arithmetic (measured, worst case).** A single plan of
20 variations × Fill-dev at 40.4 s = **808 s ≈ 13.5 min**, against the worker's
15 min limit — the *existing* Bedrock path already lives with this because it is
one call per task in a loop. Self-hosted FLUX therefore does **not** require a new
execution model at ≤20 variations, but the margin is thin:

- 20 × 40.4 s = 808 s → 89 % of the 900 s budget. Any cold start (≥120 s) inside
  the same invocation **breaks it**.
- Consequence: for the **on-demand** Availability_Mode the worker must not absorb
  the cold start synchronously. Two acceptable patterns: (a) SageMaker **async**
  invocation, where the queue absorbs the scale-up and the worker consumes S3
  results across invocations; (b) a **pre-warm-then-resume** split — the worker
  triggers scale-up, returns, and an EventBridge/Step Functions retry resumes the
  plan once the endpoint reports InService. Detailed error-taxonomy and
  invariant-preservation design is in `integration-proposal.md` (task 8.2).
- For the **always-on** mode, the existing synchronous per-task loop is adequate
  at ≤20 variations; above that, chunk the plan across worker invocations (the
  `generation_pass` guard already makes resumption safe).

## 5. Ranking per Availability_Mode (Req 3.6)

Ranking is for the shortlist that matters — the mask-inpainting path, i.e.
**FLUX.1-Fill-dev** (quality leader, non-commercial licence) and
**FLUX.1-schnell** (Apache 2.0, community-grade inpaint).

### 5a. Always-on

| Rank | Hosting_Option | Rationale |
|---|---|---|
| **1** | **SageMaker real-time endpoint** (ml.g6e.2xlarge, or ml.g6e.4xlarge resident-bf16) | Managed TLS + SigV4 auth + IAM-scoped invoke, no VPC attachment for the Lambda, no self-built orchestration. Measured 36.7 s Fill-dev fits the 60 s invocation ceiling. Quota is already in place (ml.g6e.2xlarge = 4). Highest-cost option per hour but the operationally cheapest |
| 2 | **EC2 + inference server** | Exactly what Phase C measured, so zero packaging risk and ~26 % cheaper per hour than the equivalent ml.* rate (g6e.2xlarge $2.242 vs ml.g6e.2xlarge $2.80). Costs: you own AMI/patching, health checks, TLS, token auth, and the Lambda must join the VPC |
| 3 | **ECS/EKS GPU service** | Only wins if the portal already ran a GPU cluster (it does not — the portal is Lambda + DynamoDB + CDK). Adds a cluster, a 15–25 GB image pipeline, and node lifecycle for no measured benefit |
| 4 | **SageMaker async** | Works always-on with a fixed instance count, but the S3-in/S3-out indirection is pure overhead when capacity is already warm |
| 5 | **SageMaker JumpStart** | No curated entry for any shortlisted model; reduces to option 1 with extra indirection |

### 5b. On-demand (scale-to-zero)

| Rank | Hosting_Option | Rationale |
|---|---|---|
| **1** | **SageMaker async inference** | The only option where a request arriving at **zero** instances is queued rather than rejected — the cold start becomes a latency event, not an error path, which is exactly what a dev/test environment wants. No 60 s invocation ceiling, so it also covers slower models. Estimated 4–10 min first-request latency is acceptable for a human-in-the-loop preview workflow |
| **2** | **EC2 stop/start orchestration** | Fastest estimated cold start (2–4 min) because weights stay on the EBS volume, and cheapest while stopped (EBS only). Costs an EventBridge + Lambda state machine, an idle detector, and a readiness poll you must build and test |
| 3 | **SageMaker real-time with scale-to-zero** | Managed mechanism exists, but a request at zero instances is not transparently queued — the caller must pre-warm and poll, which is the same orchestration burden as EC2 stop/start without the EBS-resident-weights speed advantage |
| 4 | **ECS/EKS with Karpenter scale-to-zero** | Mechanism is sound and queue-driven scaling (KEDA) is idiomatic, but node provision + large image pull puts it at the slow end (5–12 min) and it carries the highest fixed operational cost |
| 5 | **SageMaker JumpStart** | Real-time semantics, no shortlist coverage |

### 5c. Recommendation carried into the Decision_Record

- **Production / always-on:** SageMaker real-time endpoint on **ml.g6e.2xlarge**
  (upgrade to ml.g6e.4xlarge resident-bf16 if the measured 37 s offload ceiling
  proves too slow for reviewers), with `ml.g6e.4xlarge for endpoint usage` raised
  to ≥2 first.
- **Dev / on-demand:** SageMaker **async** endpoint on **ml.g6e.2xlarge** with
  `MinCapacity=0`, backlog-based scaling, and the worker consuming S3 outputs.
- **EC2 + inference server** stays the documented fallback: it is the only shape
  with measured evidence end-to-end and the cheapest per hour, at the price of
  owning orchestration and auth.
- **Unmeasured risk to close in implementation:** real SageMaker scale-from-zero
  cold start for a ~34 GB FLUX pipeline. Every number in §1's SageMaker rows is an
  estimate; measuring it needs one short-lived endpoint (quota is available now —
  `ml.g6e.2xlarge` = 4) and is listed as a prerequisite in `decision-record.md`.
