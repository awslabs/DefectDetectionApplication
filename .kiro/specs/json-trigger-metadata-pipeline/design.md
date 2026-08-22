# Design Document

## Overview

This feature delivers three coordinated pieces:

1. **Automated integration test** (Requirements 1–5): an in-process, end-to-end test of the
   MQTT JSON trigger → `custom_python_source` → `model_inference` → `metadata` →
   `mqtt_publish` pipeline, built entirely on the workflow engine's existing injectable
   seams so it runs in the standard `test/backend-test` pytest suite with no hardware,
   broker, GStreamer, or network.
2. **On-device verification runbook** (Requirement 6): a document plus ready-to-import
   workflow definition and dual-path handler script for verifying the same pipeline on
   real edge hardware over the Greengrass IPC / AWS IoT Core transport, satisfying the
   workspace rule that on-device features are verified on real hardware before commit.
3. **Configurable system prompt** (Requirements 7–8): a new optional `system_prompt`
   catalog parameter on both LLM inference nodes — `bedrock_inference` (threaded to the
   Bedrock Converse API `system` parameter) and `llm_inference` (threaded through the
   executor invoker, the device Text_Generation_API, and the vLLM runtime as a
   system-role chat message).

**Design invariant (backward compatibility):** when `system_prompt` is absent, empty, or
whitespace-only, every outgoing artifact — the Converse API kwargs, the
Text_Generation_API request body, the vLLM chat messages, the fallback prompt strings,
and the text-only engine prompt — stays **byte-identical** to today's behavior. Existing
packaged workflows and injected test fakes are unaffected.

All code is Python, following the existing patterns in
`src/backend/workflow_engine/` and `src/backend/vllm_runtime/`.

## Architecture

### Pipeline under verification

```
MQTT message (simulated / Greengrass IPC)
      │
      ▼
mqtt_subscribe trigger ──► Trigger_Context {topic, payload, qos, timestamp}
      │                        persisted as workflow_executions.trigger_context_json
      ▼
WorkflowActivationCore.fire ──► run_starter ──► WorkflowExecutor.execute
      │
      ▼
load_trigger_context ──► Trigger_Context + payload_json (pre-parsed when valid JSON)
      │                        seeded into Run_Metadata under "trigger"
      ▼
custom_python_source (CustomPythonBridge subprocess)
      │   produce request carries the Trigger_Context;
      │   Handler_Script extracts the image (base64 field wins over URI field),
      │   returns frame + producer metadata → Run_Metadata["python_source.<nodeId>"]
      ▼
model_inference (bridged pipeline) ──► inference results in Run_Metadata
      │
      ▼
metadata node ──► resolve_metadata_binding against trigger["payload_json"]
      │                attaches Correlation_Metadata to the output payload
      ▼
mqtt_publish ──► Output_Message = inference results + Correlation_Metadata
```

### Integration test seams (Requirement 5.2 — everything in-process)

The test drives the real production code at every stage and fakes only the I/O edges,
using seams that already exist:

| Stage | Production component | Seam used by the test |
|---|---|---|
| Trigger delivery | `TriggerSubscriptionManager` / `WorkflowActivationCore.fire` (`trigger_runtime.py`) | `mqtt_transport_factory` injected fake worker; `run_starter_factory` routes activations to the test's executor synchronously |
| Run execution | `WorkflowExecutor` (`pipeline_executor.py`) | `session_factory` (in-memory SQLite, existing test pattern), `pipeline_manager_factory` (fake GstPipelineManager returning inference tag values), `bridged_pipeline_runner` (delegates frame pumping in-process) |
| Handler execution | `CustomPythonBridge` (`python_bridge.py`) | **Real subprocess** — the actual bridge + `dda_frames` helper run unmodified, matching the existing `test_workflow_python_bridge*` tests |
| Output publication | `OutputBindingProcessor` (`output_bindings.py`) | `mqtt_publisher` injected capture callable recording `(topic, payload)` |

The model-inference stage is represented by the fake pipeline manager's tag values (the
executor's normal metadata merge path); the frame delivered to it is captured by the
injected `bridged_pipeline_runner`, giving the test the observation point for the
pixel-equality assertions (Requirements 2.2, 3.2).

### System prompt data flow

```
Bedrock path:
  catalog system_prompt ─► binding parameters ─► BedrockInferenceProcessor._run_one
      ─► invoker(model, prompt, images, region, max_tokens, system_prompt)
      ─► _default_bedrock_invoker: client.converse(..., system=[{"text": sp}])   # only when non-empty

LLM/VLM path:
  catalog system_prompt ─► binding parameters ─► LlmInferenceProcessor._run_one (verbatim, no rendering)
      ─► invoker(..., system_prompt=sp)
      ─► _default_llm_invoker: body["system_prompt"] = sp                        # only when non-empty
      ─► Text_Generation_API validation (string check; empty ⇒ absent)
      ─► VllmRuntimeManager.generate/generate_stream(system_prompt=sp)
          ├─ image + multimodal: _build_multimodal_prompt inserts {"role":"system"} first
          │     (chat-template path AND both Qwen VL fallback forms)
          └─ no image: text-only engine prompt = system text ahead of user prompt
```

## Components and Interfaces

### 1. Integration test — `test/backend-test/workflow_engine/test_workflow_trigger_metadata_pipeline.py`

A single test module (plus property-test companions, see Testing Strategy) with a shared
harness:

```python
class PipelineHarness:
    """Builds the five-node compiled document, wires the injected seams,
    fires one simulated MQTT trigger, and exposes observation points."""

    def fire(self, topic: str, payload: bytes, qos: int) -> RunResult: ...

class RunResult:
    handler_context: dict        # Trigger_Context as received by the Handler_Script
                                 # (the handler echoes it into producer metadata)
    produced_frame: "np.ndarray | None"  # frame captured at the inference stage
    run_metadata: dict           # final Run_Metadata (trigger key, python_source.<id>, results)
    published: list              # [(topic, payload_str)] captured by the mqtt_publisher fake
    status: str                  # execution row status ("completed" / "failed")
    error: str | None            # recorded failure message
```

Key harness decisions:

- **Trigger delivery**: the harness registers the workflow with a
  `TriggerSubscriptionManager` built with a fake `mqtt_transport_factory` whose worker
  exposes `deliver(context)`; the test calls it with a Trigger_Context shaped exactly
  like the production MQTT worker's (topic/payload/qos/timestamp), and the injected
  `run_starter_factory` executes the run synchronously on the test thread. This
  exercises `WorkflowActivationCore.fire`, persistence of `trigger_context_json`, and
  `load_trigger_context` with production code.
- **Handler_Script**: written to a temp dir per test; it is the same dual-path script
  shipped in the runbook (see below), so the automated test and the on-device procedure
  verify the same handler logic. The handler echoes the produce-request context into its
  returned producer metadata, giving the test the `handler_context` observation point
  (Requirements 1.2–1.4) without new engine code.
- **Frame observation**: the injected `bridged_pipeline_runner` records the frame bytes
  and caps handed to it before delegating to an in-process no-op pump, then the fake
  `pipeline_manager_factory` returns canned inference tag values (e.g.
  `{"is_anomalous": False, "confidence": 0.97}`) that stand in for `model_inference`
  output.
- **Stage-labeled assertions** (Requirement 5.5): every assertion carries a message
  prefixed with its stage — `"trigger context delivery:"`, `"image extraction:"`,
  `"inference input:"`, `"output publication:"`.

Test cases (Requirements 5.3, 5.4):

| Case | Path | Asserts |
|---|---|---|
| `test_uri_path_end_to_end` | local-file URI | Req 1.1, 1.2, 1.3, 1.5, 1.6, 2.1, 2.2, 4.1, 4.3, 4.5 |
| `test_base64_path_end_to_end` | embedded base64 | Req 1.1, 1.2, 1.3, 3.1, 3.2, 4.1, 4.3 |
| `test_non_json_payload` | invalid JSON payload | Req 1.4 (payload_json is None end to end) |
| `test_base64_wins_over_uri` | both fields | Req 3.4 (frame from base64; URI not fetched) |
| `test_rejected_uri_prefix` | disallowed prefix | Req 2.3 (error names URI + restriction; run failed) |
| `test_unloadable_uri` | missing file / non-image bytes | Req 2.4 (error names source; no frame delivered) |
| `test_http_stall_times_out` | stalling local socket | Req 2.5 (bounded failure naming the URI) |
| `test_undecodable_base64` | invalid / empty / non-image | Req 3.3 |
| `test_no_image_source` | neither field | Req 3.5 |
| `test_absent_correlation_path` | missing field path | Req 4.4 (key omitted; results + other keys intact) |
| `test_collision_keeps_result_value` | colliding key | Req 4.6 |

### 2. Verification runbook — `.kiro/specs/json-trigger-metadata-pipeline/runbook.md` (plus `runbook/` assets)

Deliverables (Requirement 6):

- **`runbook/workflow.json`** — a Pipeline_Workflow definition importable through the
  backend workflow import mechanism unmodified (Req 6.1): `mqtt_subscribe`
  (topic `dda/verify/json-trigger/request`, qos 1, Greengrass target) →
  `custom_python_source` (the dual-path handler inline) → `model_inference`
  (a model already deployed on the target device; the runbook states how to substitute
  the device's model id) → `metadata` (mappings `correlation_id → correlation_id`,
  `station.line → line`) → `mqtt_publish`
  (topic `dda/verify/json-trigger/result`, Greengrass target).
- **`runbook/handler.py`** — the dual-path Handler_Script (Req 6.2), identical in logic
  to the integration test's handler:

```python
def produce_frame(context):
    import base64
    import dda_frames

    payload = context.get("payload_json") or {}
    image_b64 = payload.get("image_b64")
    image_uri = payload.get("image_uri")
    if image_b64:                       # base64 wins (Requirement 3.4)
        raw = base64.b64decode(image_b64, validate=True)
        if not raw:
            raise ValueError("image_b64 decoded to zero bytes")
        frame = dda_frames.decode_image(raw)   # cv2.imdecode wrapper; raises naming image_b64
        return frame, {"image_source": "base64"}
    if image_uri:
        frame = dda_frames.load_image(image_uri)
        return frame, {"image_source": image_uri}
    raise ValueError(
        "no image source in trigger payload: expected 'image_b64' or 'image_uri'")
```

  (If `dda_frames` lacks a bytes→frame decode helper, the handler decodes via the
  pre-imported `cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)` — the
  design keeps the handler self-contained either way; error messages name the failing
  field per Requirements 3.3/3.5.)
- **Transport and topics** (Req 6.3): Greengrass_Transport for both directions; the
  exact topic strings above are stated in the runbook and in the workflow definition.
- **Prerequisites** (Req 6.7): the `aws.greengrass.ipc.mqttproxy` accessControl in the
  deployed component configuration must authorize `aws.greengrass#SubscribeToIoTCore`
  on `dda/verify/json-trigger/request` and `aws.greengrass#PublishToIoTCore` on
  `dda/verify/json-trigger/result` (wildcards covering them are acceptable); a deployed
  inference model; device time in sync; IoT console or `aws iot-data publish` access.
- **Procedure** (Req 6.4, 6.8): numbered steps — record pre-test
  `docker inspect --format '{{.RestartCount}}'` for the backend container; import and
  deploy the workflow; publish one URI-path Trigger_Payload and one base64-path
  Trigger_Payload from the IoT MQTT test client; observe the Output_Message on the
  result topic within 60 s of each publish; confirm inference results and matching
  `correlation_id` are present. Every step states its observable pass/fail outcome.
- **Health checks** (Req 6.5): backend container running, restart count unchanged from
  pre-test, no crash/crash-loop over ≥ 10 minutes of observation after the last
  execution (`docker ps`, restart-count re-check, log scan).
- **Diagnostics** (Req 6.9): a stage-by-stage table for the no-output-within-60 s case —
  trigger subscription (trigger health endpoint / backend log "subscribed"), execution
  start (workflow_executions row created), image extraction (execution error naming the
  image field/URI), inference (execution error naming the model node), output
  publication (mqttproxy authorization errors in the Greengrass log).

### 3. Bedrock `system_prompt` (Requirement 7)

**Catalog** (`vendor/workflow_core/catalog/nodes.py`, `BEDROCK_INFERENCE`): add

```python
ParameterDescriptor(
    "system_prompt", "string", required=False, default="",
    description="Optional system-role instructions sent as the Bedrock "
                "Converse API 'system' parameter. Empty sends no system "
                "parameter (identical to previous behavior).",
)
```

Documents omitting the parameter remain valid (`required=False`, Req 7.1). The
descriptor addition is append-only within the node's parameter list.

**Invoker** (`output_bindings.py`): extend `_default_bedrock_invoker` with a trailing
optional parameter, mirroring how `image_b64`/`reference_b64` were added to
`_default_llm_invoker`:

```python
def _default_bedrock_invoker(
    model, prompt, images, region, max_tokens,
    system_prompt: Optional[str] = None,
) -> str:
    ...
    kwargs = dict(
        modelId=model,
        messages=[{"role": "user", "content": content}],
        inferenceConfig={"maxTokens": int(max_tokens)},
    )
    if system_prompt:
        kwargs["system"] = [{"text": system_prompt}]
    response = client.converse(**kwargs)
```

When `system_prompt` is `None`/empty the kwargs are byte-identical to today's call —
no `system` key ever appears (Req 7.3).

**Processor** (`BedrockInferenceProcessor._run_one`): read and normalize the parameter,
then preserve invocation arity for injected fakes (the `_default_llm_invoker`
arity-preserving pattern):

```python
system_prompt = str(parameters.get("system_prompt") or "").strip() and \
    str(parameters.get("system_prompt"))
# normalized: None when absent/empty/whitespace-only, else the raw configured text
...
if system_prompt:
    answer = self._invoker(model, prompt, images, region, max_tokens, system_prompt)
else:
    answer = self._invoker(model, prompt, images, region, max_tokens)  # pre-feature arity
```

Normalization rule: whitespace-only ⇒ treated as absent (Req 7.3); a non-empty value is
passed **verbatim** (not stripped) so the operator's text reaches the model unmodified
(Req 7.2). The anomaly-mode instruction continues to be appended to `prompt` (the user
message) exactly where it is today, before the invoker call; the system prompt is never
touched by anomaly mode (Req 7.4).

### 4. LLM/VLM `system_prompt` (Requirement 8)

**Catalog** (`LLM_INFERENCE`): same optional string `ParameterDescriptor`
("system_prompt", required=False, default "") noting that, unlike `prompt_template`,
the value is sent verbatim with no `{placeholder}` rendering (Req 8.1, 8.2).

**Processor** (`LlmInferenceProcessor._run_one`): normalize exactly like Bedrock
(absent/empty/whitespace ⇒ absent), pass verbatim. Arity preservation extends the
existing three-form dispatch with keyword-only threading:

```python
if system_prompt is None:
    # pre-feature invocations, arity byte-identical (3-, 4-, or 5-positional)
else:
    # same positional forms + system_prompt=... keyword
    text = self._invoker(model_name, prompt, parameters, image_b64,
                         reference_b64, system_prompt=system_prompt)
```

Injected pre-feature fakes keep working because the keyword is only supplied when a
system prompt is configured (Req 8.5). Anomaly mode keeps appending
`BEDROCK_JSON_INSTRUCTION` to the rendered user prompt only (Req 8.9).

**Invoker** (`_default_llm_invoker`): add trailing `system_prompt: Optional[str] = None`;
`body["system_prompt"] = system_prompt` only when non-empty — otherwise the body is
byte-identical to the pre-feature request (Req 8.2, 8.5). The 409-loading retry loop is
untouched.

**Endpoint validation** (`src/backend/endpoints/text_generation.py`,
`validate_generate_request`): after the existing field checks,

```python
system_prompt = body.get("system_prompt")
if system_prompt is not None and not isinstance(system_prompt, str):
    findings.append({
        "field": "system_prompt",
        "reason": "system_prompt must be a string when supplied",
    })
elif isinstance(system_prompt, str) and system_prompt:
    effective["system_prompt"] = system_prompt
```

- Non-string ⇒ finding naming the field and reason; the 422 findings response returns
  before any runtime call (Req 8.6). A JSON `null` is treated as absent (it arrives as
  Python `None`), consistent with "absent keeps behavior identical".
- Empty string ⇒ no finding, no `effective` entry — processed as absent (Req 8.8).
- The generate route's `_generate_kwargs` gains `system_prompt=` only when
  `effective["system_prompt"]` exists, mirroring the `image=` pattern so fakes without
  the parameter keep working and the runtime invocation stays byte-identical otherwise.

**Runtime** (`src/backend/vllm_runtime/manager.py`): thread
`system_prompt: Optional[str] = None` through `generate`, `generate_stream`, and
`_request`, then into the engine-prompt trichotomy:

1. **No image** (bare-string path): when a system prompt is present, the engine prompt
   becomes system text ahead of user text (Req 8.7):

   ```python
   if image is None:
       engine_prompt = prompt if not system_prompt else \
           "{0}\n\n{1}".format(system_prompt, prompt)
   ```

   (Text-only engines here receive a plain string today; a role-tagged template is not
   available on this path, so ordered concatenation with a blank-line separator is the
   defined "ahead of" form. Absent system prompt ⇒ the bare prompt string, unchanged.)
2. **Image + multimodal** (`_build_multimodal_prompt`): accept `system_prompt`; when
   non-empty, prepend `{"role": "system", "content": [{"type": "text", "text": system_prompt}]}`
   ahead of the user entry before `apply_chat_template` — for both the single-image and
   the two-image (reference) content forms (Req 8.3). The fallback forms gain a
   `<|im_start|>system\n{system}<|im_end|>\n` block ahead of the existing
   `<|im_start|>user…` section, leaving the vision placeholder tokens and the remainder
   of both fallback strings unchanged (Req 8.4):

   ```python
   _QWEN_VL_SYSTEM_PREFIX = "<|im_start|>system\n{system}<|im_end|>\n"
   fallback = (_QWEN_VL_SYSTEM_PREFIX.replace("{system}", system_prompt)
               if system_prompt else "") + fallback
   ```

   When `system_prompt` is absent/empty the messages list and fallback strings are
   byte-identical to today (Req 8.5).
3. **Image + text-only model**: unchanged warning path; the bare-prompt rule from (1)
   applies.

## Data Models

### Trigger_Payload schema (Requirements 3.4, 6.6)

```json
{
  "image_uri":       "optional string — file path, s3://bucket/key, or http(s):// URL",
  "image_b64":       "optional string — base64-encoded image bytes (PNG/JPEG); wins over image_uri",
  "correlation_id":  "string — echoed into the Output_Message by the metadata node",
  "station":         { "line": "string — example nested field demonstrating dotted paths" }
}
```

At least one of `image_uri` / `image_b64` must be present; both present ⇒ `image_b64`
wins and `image_uri` is never fetched.

### Output_Message (Requirement 4)

JSON object: the workflow-result payload rendered by the `mqtt_publish` binding
(inference results such as `is_anomalous`, `confidence`) merged with the metadata-node
attachments top-level. Collisions keep the workflow-result value (existing merge rule).
Correlation keys resolving to JSON `null` appear with value `null`; unresolvable paths
are omitted.

### Converse API invocation (Requirement 7)

```python
# system_prompt configured (non-empty):
client.converse(modelId=..., messages=[{"role": "user", "content": [...]}],
                inferenceConfig={"maxTokens": ...},
                system=[{"text": system_prompt}])
# otherwise: identical call with NO system key.
```

### Text_Generation_API request body (Requirement 8)

```python
{"prompt": str, "max_tokens": int?, "temperature": float?, "top_p": float?,
 "image": str?, "reference_image": str?,
 "system_prompt": str?}   # present only when configured non-empty
```

## Error Handling

- **Handler errors** (Req 2.3–2.5, 3.3, 3.5): the Handler_Script raises `ValueError`
  with a message naming the failing source (`image_b64` / the URI / "no image source");
  the existing `CustomPythonBridge` error path converts this into
  `CustomPythonNodeError` naming the node, and the executor records the execution
  failed without invoking the inference stage. The `dda_frames` prefix gate and HTTP
  timeout already produce errors naming the URI and restriction; no engine changes.
- **Metadata resolution** (Req 4.4): `resolve_metadata_binding` never raises —
  unresolved paths log and omit; the run continues (existing behavior, asserted by the
  test).
- **Bedrock invocation failures**: unchanged — `BedrockInferenceError` naming the node;
  the system prompt adds no new failure mode (a non-string catalog value is coerced by
  the existing `str(...)` parameter handling).
- **Text_Generation_API validation** (Req 8.6): non-string `system_prompt` ⇒ 422
  `{"findings": [{"field": "system_prompt", "reason": ...}]}`, runtime never invoked.
  Empty string ⇒ silently treated as absent (Req 8.8), consistent with omitted optional
  generation parameters never being findings.
- **Runtime**: system-prompt threading introduces no new exception paths; chat-template
  failure keeps the existing warn-and-fall-back behavior with the system-aware fallback
  form (Req 8.4).

## Testing Strategy

All tests live under `test/backend-test/` and run in the standard pytest invocation
(Req 5.1). No hardware, broker, GStreamer, boto3 network calls, or vLLM wheel needed:
Bedrock/LLM tests use captured-kwargs fakes; runtime tests use the existing fake-engine
/ fake-tokenizer patterns from the vllm_runtime test modules; pipeline tests use the
seams listed in Architecture with the **real** `CustomPythonBridge` subprocess.

- **Integration examples**: the table in Components §1 (Requirements 1–5).
- **Bedrock unit tests** (Req 7.5): complete converse kwargs asserted for non-empty
  system prompt, absent parameter, empty and whitespace-only values, and anomaly mode +
  system prompt.
- **LLM/VLM unit tests** (Req 8.10): invoker body with/without system prompt and with
  anomaly mode; runtime messages with/without system prompt (single- and two-image);
  fallback form with system prompt; text-only engine prompt; endpoint empty-string
  treatment; endpoint non-string rejection.
- **Property tests**: implement the Correctness Properties below with Hypothesis
  (already a suite dependency), minimum 100 examples each, each tagged
  `Feature: json-trigger-metadata-pipeline, Property N`. Properties 2 runs through the
  real bridge subprocess (bridge reused across examples, matching the existing
  `test_property_python_bridge_*` pattern); all others run against pure functions or
  captured-kwargs fakes.
- **Runbook**: executed manually on a real device (JP5/JP6) before commit, per the
  workspace on-device verification rule; the runbook itself records what was verified.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid
executions of a system — essentially, a formal statement about what the system should
do. Properties serve as the bridge between human-readable specifications and
machine-verifiable correctness guarantees.*

### Property 1: Trigger payload parsing is total and faithful

For any trigger payload string: if the payload is valid JSON, `load_trigger_context`
yields a Trigger_Context whose `payload_json` equals the parsed document; if it is not
valid JSON, `payload_json` is None — and in both cases the `topic`, `payload`, and
`qos` fields round-trip unchanged through persistence.

**Validates: Requirements 1.3, 1.4**

### Property 2: Image acquisition preserves pixels

For any image (arbitrary small dimensions and pixel values, losslessly encoded) and
either acquisition path — a URI reference or a base64-embedded field in the
Trigger_Payload — the frame the Handler_Script produces through the real
CustomPythonBridge is pixel-for-pixel identical (equal dimensions, equal pixel values)
to decoding the original image bytes directly.

**Validates: Requirements 2.2, 3.2**

### Property 3: Correlation metadata resolution is exact

For any JSON object trigger payload and any set of metadata mappings: every mapping
whose dotted field path resolves in the payload attaches its key with a value equal to
the value at that path (a JSON null resolves to an attached null, distinguishable from
omission), and every mapping whose field path does not resolve omits its key while all
other resolved entries remain attached.

**Validates: Requirements 4.2, 4.3, 4.4, 4.5**

### Property 4: Workflow-result keys win collisions

For any workflow-result output payload and any attached metadata map, every key present
in both retains the workflow-result value in the rendered Output_Message, and every
non-colliding attached key appears with its resolved value.

**Validates: Requirements 4.6**

### Property 5: Bedrock system parameter shape

For any non-empty (at least one non-whitespace character) system prompt and any user
prompt, the Converse API invocation carries `system=[{"text": <system prompt verbatim>}]`
(exactly one block), and every other argument — modelId, the user-role message content
including labeled image blocks, and inferenceConfig — equals the invocation produced
with no system prompt configured.

**Validates: Requirements 7.2**

### Property 6: Bedrock absent/empty invariance

For any system prompt value that is absent, empty, or consists only of whitespace
characters, the Converse API invocation arguments are byte-identical to the
pre-feature invocation, with no `system` key present, and the injected-invoker call
keeps the pre-feature arity.

**Validates: Requirements 7.3**

### Property 7: Bedrock anomaly mode touches only the user prompt

For any user prompt and any configured system prompt, with anomaly mode enabled, the
user-role message text equals the user prompt with the Anomaly_Mode_Instruction
appended at the end, and the system prompt passed to the invoker equals the configured
value verbatim (unmodified, un-appended, un-replaced).

**Validates: Requirements 7.4**

### Property 8: LLM request body carries the system prompt verbatim

For any non-empty system prompt string — including strings containing `{` and `}`
characters that would fail placeholder rendering — the Text_Generation_API request body
built by the invoker contains a `system_prompt` field equal to the configured value
verbatim, and all remaining body fields (`prompt`, generation parameters, `image`,
`reference_image`) equal those of the invocation produced with no system prompt.

**Validates: Requirements 8.2**

### Property 9: LLM absent/empty invariance at every layer

For any system prompt value that is absent, empty, or whitespace-only: the invoker
request body is byte-identical to the pre-feature body (no `system_prompt` key), the
Text_Generation_API treats an empty-string field as absent (no finding, no runtime
kwarg), and the VLM_Runtime builds messages, fallback prompts, and engine prompts
byte-identical to the pre-feature forms.

**Validates: Requirements 8.5, 8.8**

### Property 10: System text precedes user text at every prompt-construction site

For any non-empty system prompt and any user prompt, at each of the three
prompt-construction sites the system text appears ahead of the user content:
(a) the chat messages handed to `apply_chat_template` begin with a system-role entry
carrying the system prompt followed by the user-role entry, for both the single-image
and two-image forms; (b) when no chat template exists or applying it fails, the
fallback prompt contains the system text before the user content while the vision
placeholder tokens and the remainder of the fallback form are unchanged; (c) the
text-only engine prompt (no image) contains both texts with the system text first.

**Validates: Requirements 8.3, 8.4, 8.7**

### Property 11: Non-string system_prompt is rejected before generation

For any non-string JSON value (number, boolean, array, object) supplied as the
`system_prompt` field, request validation returns findings including one naming the
`system_prompt` field with a reason, the endpoint responds 422, and the runtime's
generate is never invoked.

**Validates: Requirements 8.6**

### Property 12: LLM anomaly mode touches only the user prompt

For any rendered user prompt and any configured system prompt, with anomaly mode
enabled on an LLM_Inference_Node, the prompt passed to the invoker equals the rendered
user prompt with the Anomaly_Mode_Instruction appended, and the system prompt reaches
the invoker verbatim and unmodified.

**Validates: Requirements 8.9**
