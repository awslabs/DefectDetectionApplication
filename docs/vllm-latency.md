# vLLM Workflow Latency Guide

Status: Guidance current; measured sections are skeletons pending device
measurement (spec `vllm-workflow-latency-optimization`, tasks 10.x)
Target device class: Jetson Thor (JP7), LocalServer, Qwen3-VL-8B deployed

This is the on-device documentation for tuning the latency of deployed
workflows that include a vLLM (LLM inference) node. Baseline evidence
(execution `9c98f4b7-d4df-4d7e-b565-9f2c1f945a57`, ~17.5 s end to end): the
GStreamer pipeline completes in ~30 ms, orchestration and output publishing
cost ~100 ms combined, and the generation call takes 17.43 s — more than 99%
of the run. Every lever below therefore targets the generation call itself.
Per-request evidence comes from the Generation_Phase_Breakdown emitted into
each run log (queueing / prefill / decode times plus prompt, image, and
output token counts).

## 1. Output token budget (`max_tokens`)

The LLM inference node applies an output token budget to every generation
call:

- **Default: 256 tokens.** When the node's `max_tokens` parameter is unset,
  the binding sends an explicit `max_tokens` of 256. When the configured
  value is invalid (non-numeric or non-positive), the binding substitutes the
  256-token default and logs the substitution — including the rejected
  value — to the run log.
- **Latency effect.** Decode time grows roughly linearly with the number of
  output tokens: each generated token costs one decode step at the model's
  decode rate (tokens/second). An unbounded ~250-token freeform description
  is what produced the 17.43 s baseline generation call; the output token
  budget caps that decode cost.
- **Guidance: use verdict-style bounded outputs.** If the workflow needs a
  short verdict (pass/fail, defect class, yes/no plus a phrase), set
  `max_tokens` to approximately **20–30 tokens**. This minimizes decode time
  — at a fixed decode rate, ~25 output tokens cost roughly a tenth of the
  256-token default's decode time. Pair the budget with a prompt that asks
  for a short answer so the model finishes naturally within the budget.
- **Truncation is visible.** When a generation ends because the budget was
  reached, the Generation_Phase_Breakdown line in the run log states that
  the output was truncated at the output token budget. If you see routine
  truncation, either raise the budget or tighten the prompt.

Note: only the workflow LLM inference node applies this resolution. Direct
callers of the text-generation endpoint keep the endpoint's pre-existing
default behavior unchanged.

## 2. Prefix caching (repeat-run prefill reduction)

vLLM's prefix caching (`enable_prefix_caching`) reuses computed KV-cache
blocks for repeated prompt prefixes across requests, so steady-state runs of
the same workflow skip the prefill work for the shared system prompt and
static user-prompt portion.

### 2.1 How to enable

Set `enable_prefix_caching: true` in the model component's `model.json`
engine arguments (the `1/model.json` object is passed to the vLLM engine
unfiltered):

```json
{
  "model": "/path/to/qwen3-vl-8b",
  "max_model_len": 8192,
  "gpu_memory_utilization": 0.85,
  "limit_mm_per_prompt": {"image": 2},
  "enable_prefix_caching": true
}
```

At model load, the backend application log writes an INFO entry naming the
model and stating that prefix caching is active. If the flag is absent or
false, the engine is constructed without prefix caching (pre-feature
behavior).

### 2.2 Measured first-run vs repeat-run prefill — TO BE FILLED (task 10.5)

> Skeleton — to be filled during device measurement. Values come from the
> Generation_Phase_Breakdown of two consecutive runs sharing an identical
> prompt prefix of at least 100 tokens on the target device.

| Run | Execution ID | Prompt tokens | Prefill (ms) | Decode (ms) | Notes |
|---|---|---|---|---|---|
| First run after load | _(to fill)_ | _(to fill)_ | _(to fill)_ | _(to fill)_ | cold prefix cache |
| Repeat run (same prefix) | _(to fill)_ | _(to fill)_ | _(to fill)_ | _(to fill)_ | warm prefix cache |

### 2.3 Memory tradeoff — TO BE FILLED (task 10.5)

> Skeleton — to be filled during device measurement. Prefix caching retains
> KV-cache blocks across requests, which raises steady-state KV-cache
> occupancy. Record here the measured memory effect on the target device and
> the memory preflight (memory_budget) outcome. If enabling the flag causes
> the preflight to reject the configuration, the rejection is reported
> through the existing preflight failure path (with a backend-log fallback
> notification) and must be recorded here as the measured outcome.

## 3. Image resolution vs prefill cost

For multimodal calls, the input image contributes image tokens to the
prompt, and prefill time grows with the image token count. Larger captures
mean more image tokens and longer prefill.

The LLM inference node's optional `max_image_dimension` parameter bounds
this: when a captured frame's longer edge exceeds the configured maximum,
the binding downscales it (aspect ratio preserved) before the request;
smaller frames are never upscaled, and leaving the parameter empty sends
frames unmodified. The Generation_Phase_Breakdown reports the image token
count of the image actually sent.

### 3.1 Measured resolution vs prefill — TO BE FILLED (task 10.6)

> Skeleton — to be filled during device measurement, covering at least two
> resolutions that differ by at least 2x in total pixel count, on the
> deployed vision-language model.

| Resolution (W×H) | Total pixels | `max_image_dimension` | Image tokens | Prefill (ms) | Execution ID |
|---|---|---|---|---|---|
| _(to fill — full capture)_ | _(to fill)_ | unset | _(to fill)_ | _(to fill)_ | _(to fill)_ |
| _(to fill — ≥2x fewer pixels)_ | _(to fill)_ | _(to fill)_ | _(to fill)_ | _(to fill)_ | _(to fill)_ |

## 4. Model variant guidance

This section records measured data for the deployed model and at least one
lower-latency variant (a quantized build such as AWQ/FP8, or a smaller
model), so the output-quality-vs-latency tradeoff is decided on evidence.
The deployed model choice remains a registry decision — nothing here
mandates a change.

**Comparability rule:** every variant below is measured with the **same
workflow configuration, prompt, input image, and output token budget** as
the deployed model, so the reported numbers are directly comparable. Decode
rate and end-to-end latency are derived from the Generation_Phase_Breakdown
and per-node timing data of at least one completed workflow run per model.

### 4.1 Deployed model: Qwen3-VL-8B — TO BE FILLED (task 10.8)

> Skeleton — to be filled during device measurement.

- **Packaging:** deployed model component (registry). Record the component
  name/version used for the measurement here. _(to fill)_
- **Engine arguments used** (`model.json`): _(to fill — verbatim JSON)_

```json
{}
```

- **Measured decode rate (tokens/s):** _(to fill, from
  Generation_Phase_Breakdown: output tokens ÷ decode time)_
- **Measured end-to-end latency (representative workflow run):** _(to fill,
  with execution ID)_
- **Verbatim generated output** (representative prompt, identical input
  image and sampling parameters):

```text
(to fill — verbatim)
```

### 4.2 Variant: _(name — e.g. Qwen3-VL-8B AWQ or smaller Qwen-VL build)_ — TO BE FILLED (task 10.8)

> Skeleton — to be filled during device measurement. Duplicate this section
> per additional measured variant.

- **Packaging steps** (how to stage this variant as a model component,
  without changing the deployed model): _(to fill — source of weights,
  quantization/export step if any, model component layout `1/model.json` +
  weights, publish/deploy steps)_
- **Engine arguments used** (`model.json`): _(to fill — verbatim JSON,
  including any quantization settings)_

```json
{}
```

- **Measured decode rate (tokens/s):** _(to fill)_
- **Measured end-to-end latency (representative workflow run):** _(to fill,
  with execution ID)_
- **Verbatim generated output** (same prompt, input image, and sampling
  parameters as §4.1):

```text
(to fill — verbatim)
```

- **Observed output-quality differences vs deployed model** (based on the
  recorded verbatim outputs): _(to fill)_
- **Load / preflight failure outcome** (only if applicable): if this variant
  fails to load or fails the memory preflight on the target device, record
  that outcome here — with the reported load error or preflight rejection
  reason — as the measurement result in place of the decode-rate and latency
  measurements. _(to fill or n/a)_

### 4.3 Comparison summary — TO BE FILLED (task 10.8)

| Model | Decode rate (tok/s) | End-to-end latency | Memory | Quality notes |
|---|---|---|---|---|
| Qwen3-VL-8B (deployed) | _(to fill)_ | _(to fill)_ | _(to fill)_ | baseline |
| _(variant)_ | _(to fill)_ | _(to fill)_ | _(to fill)_ | _(to fill)_ |

## 5. Reading the evidence

- **Generation_Phase_Breakdown (run log):** one INFO line per LLM inference
  call with queueing / prefill / decode milliseconds, prompt / image /
  output token counts, and a truncation statement when the output token
  budget was reached. Fields the engine does not expose are marked
  `unavailable`; the image token count is `n/a` for text-only calls.
- **Per-node timing:** the run status graph and run log show each node's
  wall-clock duration; pipeline nodes reach their terminal status at
  pipeline end-of-stream, so their durations reflect actual pipeline
  activity rather than the whole run.
- **Latency report:** the before/after comparison, per-optimization
  attribution, and residual decode-rate floor live in the spec artifact
  `.kiro/specs/vllm-workflow-latency-optimization/latency-report.md`.
