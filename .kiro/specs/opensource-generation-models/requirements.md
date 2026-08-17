# Requirements Document

## Introduction

This spec covers an **exploration and planning effort only** — no production implementation. It plans how to get open-source image generation models running on the portal AWS account (164152369890, us-east-1) alongside the DDA portal (edge-cv-portal), to extend the synthetic defect data generation feature beyond Bedrock-hosted models. In-region Bedrock currently offers only Amazon Nova Canvas for this pipeline: Titan Image Generator is retired, and Stability model access has not been granted (that work is captured in the on-hold `stability-generation-models` spec).

The candidate open-source models are FLUX.1 (dev and schnell variants) and FLUX.2 from Black Forest Labs, HunyuanImage from Tencent, and PixArt-alpha / PixArt-Sigma. The deliverables of this exploration are evaluations, hands-on benchmarks, cost analyses, and design proposals culminating in an architecture decision record that will feed a future implementation spec.

Verified current-system context the exploration builds on:

- The synthetic data pipeline's pure logic lives in `edge-cv-portal/backend/functions/synthetic_core.py`: a static `MODEL_CATALOG` with capability flags (`text_to_image`, `inpainting`, `image_variation`, `seed`, `cfg_scale`), `max_images_per_call`, and `randomization_defaults`; `filter_available_models`; deterministic `derive_task_seed(base_seed, task_index)` with `SEED_MODULUS = 858_993_460`; `build_generation_plan`; `bbox_from_mask` / `bbox_from_diff` auto-annotation; and Ground Truth manifest record building.
- `synthetic_data.py` provides the Bedrock `invoke_model` I/O, filters availability via `bedrock:ListFoundationModels` (IMAGE output modality), and uses inpainting as the primary generation path with image variation as fallback.
- The on-hold `stability-generation-models` spec defines the Provider / Request_Adapter vocabulary (per-provider request/response shaping keyed off model id). This exploration generalizes that concept to a self-hosted provider whose adapter calls a SageMaker or HTTP endpoint instead of Bedrock `invoke_model`, preserving the same pipeline invariants: Task_Seed determinism, per-preview metadata (model id, seed, resolved prompt), Mask_Region recording for auto-annotation, and Nova Canvas non-interference.
- Infrastructure is AWS CDK (`edge-cv-portal/infrastructure`) with a Lambda backend and React frontend.

Scope decisions from clarification:

- **Availability modes are user-configurable per model and environment**: production likely always-on endpoints, dev/testing likely on-demand/scale-to-zero. Hosting options must be evaluated against both modes.
- **Licensing is a decision item, not an upfront exclusion**: all candidates are benchmarked, including FLUX.1-dev (non-commercial license). The decision record makes licensing disposition explicit and flags legal review. FLUX.1-schnell is Apache 2.0; PixArt models are openly licensed.
- **Hands-on benchmarking**: temporary GPU infrastructure is stood up on the account, each candidate is tested against representative defect-generation prompts for inpainting quality, latency, and real cost, then torn down. Inpainting support is the make-or-break question.
- **The future production design targets a dynamic model registry** managed via a portal admin UI backed by a database, replacing or augmenting the static `MODEL_CATALOG`. This exploration produces the registry design proposal.

## Glossary

- **Exploration**: The evaluation and planning effort defined by this spec, executed by an engineer on the portal AWS account; the actor for all requirements in this document.
- **Portal**: The edge-cv-portal web application (React frontend, Lambda backend, CDK infrastructure) running in the Portal_Account.
- **Portal_Account**: AWS account 164152369890, region us-east-1.
- **Synthetic_Data_Generator**: The existing portal subsystem that produces synthetic defect images from source images using image generation models.
- **Candidate_Model**: One of the open-source image generation models under evaluation: FLUX.1-dev, FLUX.1-schnell, FLUX.2, HunyuanImage, PixArt-alpha, PixArt-Sigma.
- **Evaluation_Matrix**: The deliverable document comparing every Candidate_Model across capabilities, licensing, resource needs, and weights availability.
- **Benchmark_Protocol**: The written procedure defining how each Candidate_Model is benchmarked on temporary infrastructure, including prompts, metrics, cost caps, and teardown steps.
- **Benchmark_Run**: One execution of the Benchmark_Protocol against one Candidate_Model on one Hosting_Option, producing recorded results.
- **Benchmark_Infrastructure**: Temporary GPU instances or endpoints stood up in the Portal_Account solely for Benchmark_Runs.
- **Hosting_Option**: A candidate hosting architecture for serving a Candidate_Model: SageMaker real-time endpoint, SageMaker asynchronous inference, SageMaker JumpStart, EC2 GPU instance with an inference server (for example diffusers or ComfyUI behind an API), or ECS/EKS GPU service.
- **Availability_Mode**: The operating mode of a hosted model endpoint: always-on (persistent capacity) or on-demand (scale-to-zero or start-on-request).
- **Cold_Start_Time**: The elapsed time from a request arriving at an idle on-demand endpoint to the first successful generation response.
- **GPU_Quota**: The Portal_Account's service quotas for the GPU instance types a Hosting_Option requires.
- **Cost_Model**: The deliverable estimating monthly cost per Candidate_Model, Hosting_Option, Availability_Mode, and usage profile.
- **Usage_Profile**: A defined generation workload level (images per day and concurrency assumptions) used to parameterize the Cost_Model.
- **Cost_Cap**: The maximum spend authorized for all Benchmark_Infrastructure during the Exploration.
- **Model_Registry_Proposal**: The deliverable design proposal for a dynamic, database-backed model registry with a portal admin UI that replaces or augments the static `MODEL_CATALOG` in `synthetic_core.py`.
- **Integration_Proposal**: The deliverable design proposal for integrating self-hosted models into the Synthetic_Data_Generator via a Selfhosted_Provider adapter.
- **Selfhosted_Provider**: A generalization of the Provider concept from the `stability-generation-models` spec: a provider whose Request_Adapter invokes a SageMaker or HTTP endpoint instead of Bedrock `invoke_model`.
- **Request_Adapter**: Per-provider logic that maps a generation task (source image, mask input, resolved prompt, seed, randomization parameters) to a provider's request body and extracts the generated image from the provider's response.
- **Task_Seed**: The deterministic per-task seed produced by `derive_task_seed(base_seed, task_index)` in `synthetic_core.py`.
- **Mask_Region**: The rectangular region (left, top, width, height) recorded on a preview image when generation constrained the defect region; the auto-annotator derives bounding boxes from it.
- **Pipeline_Invariants**: The behaviors the Integration_Proposal must preserve: Task_Seed determinism, per-preview metadata recording (model id, seed, resolved prompt), Mask_Region recording for auto-annotation, and unchanged Nova Canvas behavior.
- **Decision_Record**: The deliverable architecture decision record stating the recommended models, hosting architectures, and licensing disposition, with rationale and alternatives considered.
- **Legal_Review_Flag**: An explicit marker in the Decision_Record identifying license terms that require legal review before production use.

## Requirements

### Requirement 1: Candidate Model Evaluation Matrix

**User Story:** As a portal engineer, I want a side-by-side evaluation matrix of every candidate open-source model, so that model selection decisions rest on documented capabilities, licenses, and resource needs rather than assumptions.

#### Acceptance Criteria

1. THE Exploration SHALL produce an Evaluation_Matrix covering every Candidate_Model.
2. THE Evaluation_Matrix SHALL record for each Candidate_Model the supported generation capabilities expressed in the `MODEL_CATALOG` capability-flag vocabulary (text_to_image, inpainting, image_variation, seed, cfg_scale).
3. THE Evaluation_Matrix SHALL record for each Candidate_Model whether inpainting is supported natively, supported via an official variant or pipeline (for example FLUX.1-Fill), supported only via community tooling, or unsupported.
4. THE Evaluation_Matrix SHALL record for each Candidate_Model the license name, the commercial-use terms, and the source URL of the license text.
5. THE Evaluation_Matrix SHALL record for each Candidate_Model the parameter count, the minimum and recommended GPU memory, and the AWS GPU instance types that satisfy those memory needs.
6. THE Evaluation_Matrix SHALL record for each Candidate_Model where the model weights are obtainable, the access mechanism (open download, gated acceptance, or API-only), and any redistribution restrictions.
7. IF a Candidate_Model's weights are unobtainable for self-hosting in the Portal_Account, THEN THE Evaluation_Matrix SHALL record that finding with its evidence and mark the Candidate_Model as excluded from Benchmark_Runs.

### Requirement 2: Hands-On Benchmark Protocol and Execution

**User Story:** As a portal engineer, I want each candidate model benchmarked on real GPU infrastructure in the portal account against representative defect-generation prompts, so that quality, latency, and cost claims are proven rather than estimated.

#### Acceptance Criteria

1. THE Exploration SHALL produce a written Benchmark_Protocol before any Benchmark_Infrastructure is provisioned.
2. THE Benchmark_Protocol SHALL define a fixed set of representative defect-generation test cases that includes inpainting tasks with a source image, a mask, and a defect prompt, and text-to-image tasks with a defect prompt.
3. THE Benchmark_Protocol SHALL define the recorded metrics for each Benchmark_Run, including inpainting output quality assessment, per-image generation latency, Cold_Start_Time where the Hosting_Option supports an on-demand Availability_Mode, and actual infrastructure cost.
4. THE Benchmark_Protocol SHALL define a Cost_Cap for all Benchmark_Infrastructure and the teardown steps that remove all Benchmark_Infrastructure after Benchmark_Runs complete.
5. WHEN a Benchmark_Run executes, THE Exploration SHALL execute the Benchmark_Run on Benchmark_Infrastructure provisioned in the Portal_Account.
6. WHEN a Benchmark_Run completes for a Candidate_Model that supports inpainting per the Evaluation_Matrix, THE Exploration SHALL record inpainting results for that Candidate_Model, including generated output images and the quality assessment against the source image and mask.
7. WHEN all Benchmark_Runs for a Candidate_Model complete, THE Exploration SHALL record the measured per-image latency and the actual cost incurred for that Candidate_Model's Benchmark_Runs.
8. WHEN Benchmark_Runs conclude, THE Exploration SHALL tear down all Benchmark_Infrastructure and record confirmation that no benchmark GPU resources remain running in the Portal_Account.
9. IF accumulated Benchmark_Infrastructure spend reaches the Cost_Cap, THEN THE Exploration SHALL stop provisioning Benchmark_Runs and record which Benchmark_Runs remain incomplete.
10. IF a Candidate_Model fails to produce usable inpainting output during Benchmark_Runs, THEN THE Exploration SHALL record the failure mode and evaluate the Candidate_Model against the remaining test cases.

### Requirement 3: Hosting Architecture Comparison

**User Story:** As a portal engineer, I want a comparison of hosting architectures for self-hosted generation models on AWS, so that the future implementation can pick a hosting approach per model and environment with known tradeoffs.

#### Acceptance Criteria

1. THE Exploration SHALL produce a hosting comparison covering every Hosting_Option: SageMaker real-time endpoints, SageMaker asynchronous inference, SageMaker JumpStart, EC2 GPU with an inference server, and ECS/EKS GPU services.
2. THE Hosting comparison SHALL evaluate each Hosting_Option for support of the always-on Availability_Mode and support of the on-demand Availability_Mode, including the scale-to-zero mechanism available and its Cold_Start_Time characteristics.
3. THE Hosting comparison SHALL record for each Hosting_Option the GPU instance types required per Candidate_Model size class (PixArt-class small models, FLUX-class ~12B models, HunyuanImage-class large models).
4. THE Exploration SHALL record the Portal_Account's current GPU_Quota for each required GPU instance type in us-east-1 and identify quota increases the future implementation would need.
5. THE Hosting comparison SHALL record for each Hosting_Option the operational integration path from the Portal's Lambda backend, including invocation interface, authentication, and timeout constraints relative to generation latency.
6. THE Hosting comparison SHALL rank the Hosting_Options per Availability_Mode with a documented rationale.

### Requirement 4: Cost Model

**User Story:** As a portal operator, I want a cost model for each model, hosting option, and usage level, so that the cost of extending generation beyond Bedrock is understood before any implementation commitment.

#### Acceptance Criteria

1. THE Exploration SHALL produce a Cost_Model covering each Candidate_Model that passed Benchmark_Runs, on each viable Hosting_Option, in each Availability_Mode.
2. THE Cost_Model SHALL define at least three Usage_Profiles spanning light development use through sustained production use, with stated images-per-day and concurrency assumptions.
3. THE Cost_Model SHALL estimate monthly cost per combination of Candidate_Model, Hosting_Option, Availability_Mode, and Usage_Profile using measured Benchmark_Run latency and current us-east-1 pricing.
4. THE Cost_Model SHALL include the per-image cost of Amazon Nova Canvas on Bedrock as a comparison baseline for each Usage_Profile.
5. WHERE a Hosting_Option supports the on-demand Availability_Mode, THE Cost_Model SHALL state the Cold_Start_Time cost-versus-latency tradeoff for that combination.

### Requirement 5: Dynamic Model Registry Design Proposal

**User Story:** As a portal administrator, I want a design proposal for a dynamic model registry with an admin UI, so that future model additions do not require code changes to a static catalog.

#### Acceptance Criteria

1. THE Exploration SHALL produce a Model_Registry_Proposal as a design document.
2. THE Model_Registry_Proposal SHALL define a database-backed registry schema that expresses every field of the existing `MODEL_CATALOG` entries (model identifier, display name, capability flags, max_images_per_call, randomization_defaults) plus provider type, endpoint configuration, Availability_Mode, and an enabled/disabled state.
3. THE Model_Registry_Proposal SHALL define per-environment endpoint configuration so one registry entry can specify different Availability_Modes for production and development environments.
4. THE Model_Registry_Proposal SHALL define the portal admin UI operations for the registry: add a model, edit a model's configuration, enable a model, and disable a model.
5. THE Model_Registry_Proposal SHALL define a migration path from the static `MODEL_CATALOG` in `synthetic_core.py` that keeps existing Bedrock entries functional during and after migration.
6. THE Model_Registry_Proposal SHALL define how the existing availability filtering generalizes to registry entries, combining Bedrock regional availability checks for Bedrock-backed entries with endpoint health or reachability checks for self-hosted entries.
7. THE Model_Registry_Proposal SHALL define the authorization boundary restricting registry write operations to portal administrators.

### Requirement 6: Self-Hosted Provider Integration Proposal

**User Story:** As a portal engineer, I want a design proposal for a self-hosted provider adapter, so that self-hosted models plug into the existing generation pipeline without breaking its invariants.

#### Acceptance Criteria

1. THE Exploration SHALL produce an Integration_Proposal as a design document.
2. THE Integration_Proposal SHALL define a Selfhosted_Provider whose Request_Adapter invokes a SageMaker or HTTP endpoint, generalizing the Provider and Request_Adapter concepts from the `stability-generation-models` spec.
3. THE Integration_Proposal SHALL define the Request_Adapter mapping for each recommended Candidate_Model: generation task inputs (source image, mask input, resolved prompt, Task_Seed, randomization parameters) to the endpoint request schema, and endpoint response to image bytes in the form the generation worker consumes.
4. THE Integration_Proposal SHALL state how each Pipeline_Invariant is preserved: Task_Seed derivation via the unchanged `derive_task_seed` function, per-preview recording of model id, seed, and resolved prompt, Mask_Region recording for inpainting tasks, and byte-identical Nova Canvas request behavior.
5. THE Integration_Proposal SHALL define the error taxonomy for self-hosted endpoint failures (endpoint unreachable, endpoint cold-starting, generation failure, malformed response) and map each to the existing per-task failure recording.
6. THE Integration_Proposal SHALL address the Lambda invocation timeout constraint for generation latencies that exceed synchronous Lambda-to-endpoint budgets, including the asynchronous invocation pattern proposed for the on-demand Availability_Mode.

### Requirement 7: Licensing Disposition

**User Story:** As a portal operator, I want an explicit licensing disposition for every candidate model, so that no model reaches production use without its license terms being assessed and legal review flagged where needed.

#### Acceptance Criteria

1. THE Decision_Record SHALL state a licensing disposition for every Candidate_Model, classifying each as cleared for commercial self-hosting, requiring legal review, or unsuitable for production use.
2. THE Decision_Record SHALL attach a Legal_Review_Flag to every Candidate_Model whose license restricts commercial use, including FLUX.1-dev's non-commercial license terms.
3. THE Decision_Record SHALL record for each Legal_Review_Flag the specific license clauses requiring review and the intended production usage they must be assessed against.
4. WHILE a Candidate_Model carries an unresolved Legal_Review_Flag, THE Decision_Record SHALL exclude that Candidate_Model from the production-recommended set while retaining its benchmark results.

### Requirement 8: Final Recommendation and Decision Record

**User Story:** As a portal engineer, I want a single decision record consolidating the exploration's findings into concrete recommendations, so that a future implementation spec can start from settled decisions.

#### Acceptance Criteria

1. THE Exploration SHALL produce a Decision_Record consolidating the Evaluation_Matrix, Benchmark_Run results, hosting comparison, Cost_Model, Model_Registry_Proposal, and Integration_Proposal.
2. THE Decision_Record SHALL recommend a set of Candidate_Models for production integration, a Hosting_Option and Availability_Mode per recommended model per environment, and a rationale citing benchmark evidence for each recommendation.
3. THE Decision_Record SHALL record the alternatives considered for each recommendation and the reason each alternative was not selected.
4. THE Decision_Record SHALL list the open questions and prerequisites for the future implementation spec, including required GPU_Quota increases and unresolved Legal_Review_Flags.
5. IF the Benchmark_Runs demonstrate that no Candidate_Model meets the inpainting quality bar for the pipeline's primary path, THEN THE Decision_Record SHALL state that finding and recommend a fallback direction.

### Requirement 9: Exploration Scope Constraints

**User Story:** As a portal operator, I want the exploration confined to evaluation artifacts and temporary infrastructure, so that the production portal and its account resources remain untouched.

#### Acceptance Criteria

1. THE Exploration SHALL limit its deliverables to documents, benchmark results, and design proposals.
2. THE Exploration SHALL leave all Portal source code, including `synthetic_core.py`, `synthetic_data.py`, the frontend, and the CDK infrastructure, unmodified.
3. THE Exploration SHALL limit provisioned AWS resources to Benchmark_Infrastructure that the Benchmark_Protocol's teardown steps remove.
4. WHEN the Exploration concludes, THE Exploration SHALL confirm that the Portal's deployed stacks in the Portal_Account are unchanged from their pre-Exploration state.
