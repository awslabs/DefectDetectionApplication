# Requirements Document

## Introduction

Deployed-workflow runs that include a vLLM inference node currently take ~17.5 s end to end on jetson-thor1 (JP7, LocalServer 1.0.10, Qwen3-VL-8B). The per-node timing added by the node-execution-timing feature attributes the cost precisely for the measured run (execution 9c98f4b7-d4df-4d7e-b565-9f2c1f945a57): the GStreamer pipeline completes in ~30 ms, mqtt_publish_1 takes 2 ms, Python orchestration overhead totals ~100 ms, and the llm_inference_1 binding invocation takes 17.43 s — more than 99% of the run. The generated output was a ~250-token freeform image description with no max_tokens bound.

This feature reduces vLLM workflow latency on a best-effort basis, driven by measurement rather than hypothesis. The requirements mandate a measured phase breakdown of the generation call before any optimization, apply optimization levers to the actual bottleneck (the vLLM generation path: output token budget, prompt-prefix caching, multimodal prefill cost, per-run fixed costs), require each optimization's gain to be measured on real hardware, and cover model-level options (quantization, smaller variants) as guidance and measurement only — the deployed model choice remains a registry decision. Multi-threaded output launches and C/C++ bindings for the orchestration code are explicitly not in scope: the measured evidence shows outputs and orchestration cost ~100 ms combined, and such work becomes eligible only if later profiling evidence shows orchestration overhead is material.

The feature also fixes a profiling artifact: Pipeline_Nodes (folder_source, capture) currently show ~17.5 s durations because they reach a terminal node status only at run end rather than at pipeline EOS, which misleads exactly this kind of latency investigation.

Scope: the LocalServer backend (`src/backend` — `vllm_runtime`, `workflow_engine`, device endpoints) and on-device documentation. Any device-side change is subject to the real-hardware verification gate before commit.

## Glossary

- **LocalServer**: The Greengrass component running on an edge device that hosts the on-device backend (`src/backend`) and frontend (`src/frontend`).
- **Workflow_Run**: One triggered execution of a deployed workflow on the device, recorded as a WorkflowExecution with per-node timing from the node-execution-timing feature.
- **WorkflowExecutor**: The `workflow_engine.pipeline_executor.WorkflowExecutor` that runs the compiled pipeline and then the output bindings.
- **LLM_Binding**: The `llm_inference` output binding in `workflow_engine/output_bindings.py` that calls the Text_Generation_API, forwarding the node parameters `max_tokens`, `temperature`, and `top_p`.
- **Text_Generation_API**: The device endpoint (`src/backend/endpoints/text_generation.py`) that accepts a prompt (optionally with image data) and invokes the VLLM_Manager.
- **VLLM_Manager**: The `vllm_runtime.manager` component that loads vLLM models from staged engine arguments (`model.json` parsed into AsyncEngineArgs) and serves generate requests.
- **Generation_Call**: One invocation of the VLLM_Manager's generate path for a Workflow_Run, from request receipt to the final generated text being returned.
- **Generation_Phase_Breakdown**: A per-Generation_Call decomposition of elapsed time into at least: request queueing/scheduling time, prefill time (prompt and image token processing), and decode time (output token generation), together with prompt token count, image token count (when multimodal), and output token count.
- **Engine_Arguments**: The vLLM engine configuration parsed from a model component's `model.json` (e.g. `max_model_len`, `gpu_memory_utilization`, `limit_mm_per_prompt`, `enable_prefix_caching`).
- **Prefix_Caching**: The vLLM engine capability (`enable_prefix_caching`) that reuses computed KV-cache blocks for repeated prompt prefixes across requests, skipping their prefill cost.
- **Output_Token_Budget**: The effective `max_tokens` bound applied to a Generation_Call, limiting decode time proportionally to output length.
- **Pipeline_Node**: A workflow node that maps to GStreamer pipeline elements (e.g. folder_source, capture); frames flow through it while the pipeline plays.
- **Pipeline_EOS**: The end-of-stream event of the compiled GStreamer pipeline, after which Pipeline_Nodes perform no further work in the run.
- **Node_Execution_Time**: The per-node wall-clock duration recorded by the node-execution-timing feature and shown in the run status graph and run log.
- **Latency_Report**: A documented, measurement-backed summary for a workflow configuration stating the measured end-to-end latency, the Generation_Phase_Breakdown, and the residual latency floor attributable to model decode rate.
- **Model_Variant_Guidance**: Documented measurements and recommendations covering alternative model packaging choices (quantized builds such as AWQ/FP8, or smaller model variants) without mandating a deployed model change.

## Requirements

### Requirement 1: Measure the generation call before optimizing

**User Story:** As a developer optimizing workflow latency, I want a per-request phase breakdown of the vLLM generation call, so that every optimization targets a measured cost rather than a hypothesis.

#### Acceptance Criteria

1. WHEN a Generation_Call completes, THE VLLM_Manager SHALL record a Generation_Phase_Breakdown for that Generation_Call, including queueing/scheduling time, prefill time, decode time, prompt token count, output token count, and, for Generation_Calls whose request includes image data, image token count; for Generation_Calls whose request includes no image data, THE Generation_Phase_Breakdown SHALL mark the image token count field as not applicable rather than reporting a measured value.
2. WHEN a Workflow_Run invokes the LLM_Binding and the resulting Generation_Call completes, THE WorkflowExecutor SHALL emit that Generation_Call's Generation_Phase_Breakdown into the run log while the run's log capture is active, including any fields marked unavailable or not applicable.
3. THE Generation_Phase_Breakdown SHALL express each phase duration in milliseconds using a monotonic clock source.
4. IF the vLLM engine does not expose a metric needed for a Generation_Phase_Breakdown field, THEN THE VLLM_Manager SHALL record the fields that are available and SHALL mark the unavailable fields as unavailable rather than reporting estimated values as measurements.
5. IF recording or emitting a Generation_Phase_Breakdown raises an error, THEN THE VLLM_Manager SHALL contain the error, and the Generation_Call SHALL return its generated text and the Workflow_Run SHALL reach the same terminal state as a run in which no measurement error occurred.
6. IF a completed Generation_Call's Generation_Phase_Breakdown reports zero milliseconds for every phase simultaneously, THEN THE VLLM_Manager SHALL treat the breakdown as an instrumentation error and SHALL mark the breakdown's phase fields as unavailable rather than reporting the all-zero values as measurements.
7. THE Generation_Phase_Breakdown instrumentation SHALL add no more than 10 milliseconds to the end-to-end latency of a Generation_Call, verified on the target device by comparing the median Generation_Call latency over at least 5 identical requests with the instrumentation present against the same measurement without it.

### Requirement 2: Correct pipeline-node timing attribution

**User Story:** As an operator profiling a workflow, I want Pipeline_Node durations to reflect actual pipeline activity, so that the per-node timing data is trustworthy for latency investigations.

#### Acceptance Criteria

1. WHEN the compiled pipeline reaches Pipeline_EOS during a Workflow_Run, THE WorkflowExecutor SHALL, within 100 milliseconds of observing Pipeline_EOS, transition every Pipeline_Node that is in the `running` state and has no recorded failure to the terminal status `success` (or `warning` where the existing clean-completion lifecycle would assign `warning`), so that each such Pipeline_Node's Node_Execution_Time reflects the interval from entering `running` to Pipeline_EOS.
2. WHEN a Workflow_Run's pipeline reaches Pipeline_EOS before the run's output bindings complete, THE recorded Node_Execution_Time of each Pipeline_Node SHALL be at most that Pipeline_Node's elapsed time from entering `running` to Pipeline_EOS plus 100 milliseconds, regardless of how long the output bindings take to complete after Pipeline_EOS.
3. IF the pipeline terminates without reaching Pipeline_EOS (error or timeout), THEN THE WorkflowExecutor SHALL record each Pipeline_Node's terminal status according to the existing finalize rules of the node-status lifecycle — attributing `failure` to the failing Pipeline_Node when the failing node is identified, and resolving Pipeline_Nodes to `warning` when the failure cannot be attributed to a specific node — and every recorded Node_Execution_Time SHALL be a non-negative integer number of milliseconds matching the collector's serialization, with each Pipeline_Node reaching a terminal status.
4. THE change to Pipeline_Node terminal-status timing SHALL preserve the existing set of node statuses (`pending`, `running`, `success`, `warning`, `failure`), the existing node-status map serialization fields (`status`, optional `detail`, optional `durationMs`), the existing device API response shapes, and the existing timing behavior of non-Pipeline_Node (executor-binding) nodes.
5. WHEN a Workflow_Run completes, THE final per-node status map SHALL contain the same terminal status for every node as it would have contained before this change, differing only in when Pipeline_Nodes reach their terminal status and in the resulting Node_Execution_Time values.
6. IF transitioning Pipeline_Nodes to a terminal status at Pipeline_EOS raises an error, THEN THE WorkflowExecutor SHALL contain the error per the collector's best-effort discipline, and the Workflow_Run SHALL NOT fail because of that error and SHALL reach the same run outcome as a run in which no such error occurred.
7. IF a Pipeline_Node is still in the `pending` state when the pipeline reaches Pipeline_EOS, THEN THE WorkflowExecutor SHALL record no Node_Execution_Time for that Pipeline_Node at Pipeline_EOS, and that Pipeline_Node SHALL be resolved to its terminal status by the existing node-status lifecycle.

### Requirement 3: Bound the output token budget for workflow inference

**User Story:** As a workflow author, I want the LLM inference node to apply a sensible output token bound by default, so that decode time does not grow unbounded when I have not tuned the node.

#### Acceptance Criteria

1. WHEN a workflow's LLM inference node specifies a `max_tokens` parameter, THE LLM_Binding SHALL forward that value to the Text_Generation_API as the `max_tokens` of the Generation_Call.
2. WHEN a Generation_Call carries a `max_tokens` value forwarded by the LLM_Binding, THE VLLM_Manager SHALL enforce that value as the Output_Token_Budget of the Generation_Call, such that the output token count reported in the Generation_Phase_Breakdown never exceeds the Output_Token_Budget.
3. IF a workflow's LLM inference node specifies a `max_tokens` value that is invalid (non-numeric or non-positive), THEN THE LLM_Binding SHALL set the Output_Token_Budget of the Generation_Call to the documented default of 256 tokens and SHALL log the substitution, including the rejected value, to the run log.
4. WHEN a workflow's LLM inference node specifies no `max_tokens` parameter, THE LLM_Binding SHALL set the Output_Token_Budget of the Generation_Call to exactly the documented default of 256 tokens.
5. WHEN a Generation_Call ends because the Output_Token_Budget was reached, THE Generation_Phase_Breakdown emitted to the run log SHALL state that the output was truncated at the Output_Token_Budget.
6. WHEN a Generation_Call ends before reaching the Output_Token_Budget, THE Generation_Phase_Breakdown emitted to the run log SHALL NOT report truncation.
7. IF a Generation_Call originates from a caller other than the LLM_Binding and omits `max_tokens`, THEN THE Text_Generation_API SHALL apply pre-feature behavior unchanged, without injecting a default Output_Token_Budget.
8. THE on-device documentation for the LLM inference node SHALL state the default Output_Token_Budget of 256 tokens, its latency effect, and guidance that verdict-style bounded outputs (approximately 20–30 tokens) minimize decode time.

### Requirement 4: Reduce repeated prefill cost with prefix caching

**User Story:** As an operator running the same workflow repeatedly, I want repeated prompt prefixes to skip prefill work, so that steady-state runs are faster than first runs.

#### Acceptance Criteria

1. WHERE a model component's Engine_Arguments enable Prefix_Caching, THE VLLM_Manager SHALL construct the engine with Prefix_Caching active and SHALL write an entry to the backend application log at model load, identifying the model and stating that Prefix_Caching is active.
2. WHEN two consecutive Generation_Calls on the same loaded model share an identical prompt prefix of at least 100 prompt tokens (system prompt and static user-prompt portion), Prefix_Caching is active, and no model reload or engine reconstruction occurs between the two calls, THE VLLM_Manager SHALL serve the second Generation_Call with a measured prefill time lower than the first Generation_Call's measured prefill time, as evidenced by both Generation_Calls' Generation_Phase_Breakdowns on the target device.
3. THE Model_Variant_Guidance SHALL document how to enable Prefix_Caching in a model component's Engine_Arguments and the measured first-run versus repeat-run prefill times for the same prompt prefix on the target device, sourced from the Generation_Phase_Breakdown.
4. IF enabling Prefix_Caching causes the model's memory preflight (memory_budget) to reject the configuration on the target device, THEN THE VLLM_Manager SHALL report the rejection through the existing preflight failure path, and THE Model_Variant_Guidance SHALL document the memory tradeoff.
5. IF the existing preflight failure path fails to report a Prefix_Caching-related rejection, THEN THE VLLM_Manager SHALL write the rejection to the backend application log as a fallback notification.
6. WHEN a model component's Engine_Arguments do not enable Prefix_Caching, THE VLLM_Manager SHALL construct the engine without Prefix_Caching, preserving pre-feature behavior.

### Requirement 5: Reduce multimodal prefill cost

**User Story:** As a workflow author using image inference, I want the image contribution to prefill measured and reducible, so that image size does not dominate latency unnecessarily.

#### Acceptance Criteria

1. WHEN a multimodal Generation_Call completes, THE Generation_Phase_Breakdown SHALL include the image token count for that Generation_Call.
2. THE Model_Variant_Guidance SHALL document the measured relationship between input image resolution and prefill time on the target device for the deployed vision-language model, covering at least two measured image resolutions that differ by at least 2x in total pixel count.
3. WHERE the workflow's LLM inference node configuration provides an image downscaling option specifying a maximum pixel dimension, WHEN the captured image's longer edge exceeds the configured maximum pixel dimension, THE LLM_Binding SHALL downscale the captured image so that its longer edge equals the configured maximum pixel dimension with aspect ratio preserved before the Text_Generation_API request, and THE Generation_Phase_Breakdown SHALL report the image token count of the image actually sent.
4. IF a configured image downscaling operation fails to apply, THEN THE LLM_Binding SHALL send the original captured image, THE LLM_Binding SHALL emit a warning in the run log indicating the downscaling failure, THE Generation_Phase_Breakdown SHALL report the image token count of the original image actually sent, and the run SHALL reach the same terminal state it would reach without the downscaling failure.
5. IF no captured image is provided to the LLM_Binding for the Generation_Call, THEN THE LLM_Binding SHALL perform no downscaling operation and THE Generation_Phase_Breakdown SHALL report the image token count as zero or not applicable.
6. WHEN no image downscaling option is configured, THE LLM_Binding SHALL send the captured image unmodified, byte-identical to pre-feature behavior.
7. WHERE the workflow's LLM inference node configuration provides an image downscaling option, WHEN the captured image's longer edge is less than or equal to the configured maximum pixel dimension, THE LLM_Binding SHALL send the captured image unmodified and SHALL NOT upscale it.
8. IF the configured image downscaling value is non-positive or non-numeric, THEN THE LLM_Binding SHALL treat the downscaling option as unconfigured, send the captured image unmodified, and emit a warning in the run log indicating the invalid configuration value.

### Requirement 6: Eliminate avoidable per-run fixed costs

**User Story:** As an operator, I want each workflow run to reuse the already-loaded model with no per-run reload or warm-up, so that fixed overhead does not add to generation latency.

#### Acceptance Criteria

1. WHEN a Generation_Call arrives for a model in the READY state (the VLLM_Manager's status for a fully loaded model available to serve requests), THE VLLM_Manager SHALL serve the request from the existing loaded engine without reloading model weights, reconstructing the engine, or performing a per-run warm-up generation, as evidenced by the absence of model-load and engine-construction log entries between the request's receipt and its response.
2. WHEN two or more consecutive Workflow_Runs invoke the same model and that model remains in the READY state throughout, THE run log of each such Workflow_Run SHALL contain no model-loading or engine-construction activity attributed to the Generation_Call, and each run's Generation_Call duration recorded in the per-node timing data SHALL consist only of the Generation_Phase_Breakdown phases (queueing/scheduling, prefill, decode) with no model-load contribution.
3. THE Latency_Report SHALL state the measured non-generation overhead of a Workflow_Run on the target device, computed as the run's end-to-end duration minus the Generation_Call duration and decomposed using the per-node timing data into pipeline time, orchestration time, and output publishing time, and THE measured non-generation overhead SHALL be at most 500 milliseconds.
4. IF the measured non-generation overhead of a Workflow_Run exceeds 500 milliseconds on the target device, THEN THE Latency_Report SHALL attribute the excess to specific nodes or phases using the per-node timing data, and THE Latency_Report SHALL record that orchestration-level optimizations (such as concurrent output binding launches) become eligible as follow-up work only after that attribution is documented.

### Requirement 7: Model-level guidance and measurement

**User Story:** As a deployment decision-maker, I want measured data on quantized and smaller model variants, so that I can trade output quality against latency with evidence instead of guesses.

#### Acceptance Criteria

1. THE Model_Variant_Guidance SHALL document, for the deployed model and at least one lower-latency variant (a quantized build or a smaller model), the measured decode rate in tokens per second and the measured end-to-end latency of a representative Workflow_Run on the target device, with both values derived from the Generation_Phase_Breakdown and per-node timing data of at least one completed Workflow_Run per model.
2. THE Model_Variant_Guidance SHALL state, for each measured variant, the packaging steps required to stage that variant as a model component, including the Engine_Arguments values used for the measurement, without mandating a change to the deployed model.
3. THE Model_Variant_Guidance SHALL record, for the deployed model and each measured variant, the verbatim generated output produced for the representative workflow prompt with identical input image and sampling parameters, and SHALL state the observed output-quality differences between the deployed model and each measured variant based on those recorded outputs.
4. IF a measured variant fails to load or fails the memory preflight on the target device, THEN THE Model_Variant_Guidance SHALL record that outcome, together with the reported load error or preflight rejection reason, as the measurement result for that variant in place of the decode-rate and latency measurements.
5. THE Model_Variant_Guidance SHALL use the same workflow configuration, prompt, input image, and Output_Token_Budget for the representative Workflow_Run of the deployed model and of every measured variant, so that the reported measurements are directly comparable.

### Requirement 8: Measured outcomes and latency report

**User Story:** As the requester of this optimization, I want a documented before/after latency comparison with an evidence-backed statement of the remaining floor, so that the achieved improvement and its limits are explicit.

#### Acceptance Criteria

1. THE Latency_Report SHALL state the baseline measurement (execution 9c98f4b7-d4df-4d7e-b565-9f2c1f945a57: ~17.5 s total, 17.43 s in the Generation_Call) and the measured end-to-end latency of the same workflow after the implemented optimizations, measured on a JP7 device of the same class as jetson-thor1 running the same deployed model (Qwen3-VL-8B), reporting both the first Workflow_Run after model load and the steady-state latency taken as the median of at least 3 consecutive subsequent Workflow_Runs.
2. THE Latency_Report SHALL state, for each implemented optimization, its individually measured latency change, derived from a comparison of Workflow_Runs on the target device that differ only in that optimization, backed by Generation_Phase_Breakdown or per-node timing data from those runs.
3. THE Latency_Report SHALL state the residual latency floor for the deployed model as a function of output token count and the decode rate (in tokens per second) measured from Generation_Phase_Breakdown data on the target device, and SHALL state the measured steady-state end-to-end latency (median of at least 3 consecutive Workflow_Runs) of a bounded verdict-style Workflow_Run (Output_Token_Budget of at most 30 tokens) after optimizations.
4. WHEN the optimizations are complete, THE Latency_Report SHALL identify, for at least the baseline freeform-description configuration and the bounded verdict-style configuration, which workflow configurations reach sub-second (less than 1000 milliseconds) steady-state end-to-end latency on the target device and which do not, with the measured evidence for each.
5. IF an implemented optimization's latency effect cannot be isolated by a single-variable comparison (for example, because it is inseparable from another change), THEN THE Latency_Report SHALL state that the effect was measured in combination and SHALL identify which optimizations share the combined measurement.
6. THE Latency_Report SHALL identify, for each measured latency value it states, the Workflow_Run execution identifier(s) or run-log source from which the value was taken.

### Requirement 9: Non-regression and hardware verification

**User Story:** As a maintainer, I want the latency work to leave existing behavior intact and be verified on real hardware, so that the optimization does not trade correctness for speed.

#### Acceptance Criteria

1. THE VLLM_Manager SHALL preserve its existing generate and generate_stream semantics for all callers: for identical requests (identical prompt, identical image data, and identical sampling parameters including max_tokens) issued with deterministic sampling settings (temperature 0 or a fixed seed), identical generated-text results, and identical error types (ModelUnavailableError, GenerationError) on the existing failure paths.
2. THE Text_Generation_API and the device node-status API SHALL retain every existing request and response field with its current name, type, and meaning; new data SHALL appear only as additive fields.
3. WHEN the existing backend test suite runs with this feature present, THE test suite SHALL pass with zero failures and zero errors, with no pre-existing test assertion weakened or deleted; updates to the security-preservation golden baseline data performed through the documented baseline maintenance path SHALL NOT count as assertion modifications.
4. WHEN a device-side change from this feature is ready to commit, THE change SHALL have been verified on a real device of every JetPack architecture the change touches (with no cross-architecture assumptions) by running the affected workflow end to end to a successful Workflow_Run terminal state, with the backend remaining healthy (no crash, no container restart, no crash-loop) throughout the verification period, per the hardware verification gate, before the commit is made.
5. WHERE a device-side change is committed as an emergency hotfix or critical security patch without prior hardware verification, THE change SHALL be verified on a real device of the matching JetPack architecture within 24 hours of the commit using the same verification steps as the pre-commit gate, and the post-commit verification outcome SHALL be recorded with the change.
6. IF an optimization is enabled and a Generation_Call subsequently fails, THEN THE VLLM_Manager SHALL surface the failure through the existing failure paths with the failing model isolated, while all other loaded models remain in the READY state and continue serving Generation_Calls.
