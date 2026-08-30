# Design Document

## Overview

This feature turns the workflow engine's single-verdict inference model into
a per-detection inspection pipeline, entirely with executor-level
mechanisms. Five design decisions map one-to-one onto the requirements:

1. **Detections from the run's capture record** (Requirement 1): the
   Marshal_Model already writes every object-detection inference's
   per-detection map into the run's `.jsonl` capture record; the executor
   reads it back post-pipeline, orders it, assigns Detection_IDs, and merges
   it into the Run_Metadata. No GStreamer or proprietary-plugin change.
2. **Executor-side detection crops** (Requirement 2): `bedrock_inference`
   already consumes captured JPEG files (`capturePaths`) at the executor
   level, so a per-detection crop is a `cv2` array slice of the captured
   frame — no pipeline fan-out, no compiled-document change.
3. **Payload-sourced references** (Requirement 3): a shared fetch helper
   resolves a dotted path in the trigger payload to reference-image bytes
   (S3 / HTTP(S) / base64) at Bedrock-invocation time, bypassing the
   frame-feed singleton constraint entirely.
4. **Nested verdict namespacing** (Requirement 4): anomaly-mode results gain
   `bedrock.{nodeId}.*` keys beside the unchanged flat keys, reusing the
   nested-merge mechanism freeform mode already has.
5. **Concurrent branches with publish-on-completion** (Requirement 5): the
   Bedrock processor fans bindings out to a thread pool; each branch's
   downstream output bindings run in the branch's completion path, while
   non-branch bindings keep the existing post-run ordering.

**Posture (binding).** Purely additive: a workflow using none of the new
parameters produces byte-identical Bedrock requests, Run_Metadata (modulo
the additive `detections`/`detection_count` keys), and output behavior.
No compiler changes beyond node-descriptor parameters. No changes to the
proprietary `emltriton`/`eminfer` plugins or to the compiled GStreamer
segment shapes. The vendored `workflow_core` mirror stays byte-identical to
the Portal layer copy.

## Architecture

### Run flow (target workflow, N = 3)

```
mqtt_subscribe (greengrass)             Trigger_Context {payload_json: {refs:[{id,image},...]}}
        | fires run
        v
executor: Aravis grab --> GStreamer pipeline: appsrc -> emltriton(yolo-world) -> capture sink(s)
        |                                     \- marshal writes {capture_id}.jsonl
        v (post-pipeline, in order)
[NEW] detections.merge_detections()     tag_values["detections"] = Detection_List (sorted, ID'd)
        v
BedrockInferenceProcessor  [NEW: concurrent]
  branch bedrock_1 (crop idx 0, ref refs.0.image) -+ each branch, on completion:
  branch bedrock_2 (crop idx 1, ref refs.1.image) -+-> merge verdict (lock) -> run branch
  branch bedrock_3 (crop idx 2, ref refs.2.image) -+    output bindings (mqtt_publish_i)
        v (join)
LlmInferenceProcessor (unchanged) -> OutputBindingProcessor (non-branch bindings only)
        v
finalize: persist metadata (detections + nested verdicts included)
```

### Detection data path (Decision 1)

The Marshal_Model (`marshal_for_capture_template.py`, repo-owned) emits, for
`task=object_detection` models, a capture-record output block of
`observedContentType: json_with_base64_encoding` whose decoded payload is
`{"detections": {"0": {"class_index", "class_label", "bounding_box":
[x_min, y_min, x_max, y_max], "confidence"}, ...}}` with boxes in
**source-frame pixel coordinates** (verified on-device against a live
2642x2949 Basler frame). The em-agent broker's file-target routing lands the
record at `{output_dir}/{capture_id}.jsonl` — the contract
`run_artifacts.py` already parses for masks.

A new module `src/backend/workflow_engine/detections.py` provides:

```python
def read_detections(output_dir, capture_id) -> Optional[list]
    # Parse {capture_id}.jsonl, find the json_with_base64_encoding block
    # whose decoded payload has a top-level "detections" map (the same
    # discriminator inference_results_utils uses); None when no record
    # or no detections block exists (non-detection model / no record).

def build_detection_list(raw, sort_order) -> list
    # Normalize each entry to the Data Model shape, sort per
    # Detection_Sort_Order, assign Detection_IDs (uuid4().hex[:8],
    # re-drawn on collision within the run).

def resolve_sort_order(graph_document) -> str
    # detection_sort_order from the registration's workflow.json
    # model_inference node (default "left_to_right").
```

**Call site**: `pipeline_executor.WorkflowExecutor.execute`, immediately
after `_repair_capture_artifacts(execution)` and **before** the Bedrock
processor block. Merges `tag_values["detections"]` and
`tag_values["detection_count"]` (never overwriting TAG-produced keys, which
never produce these names). Absence of a record or block leaves the keys
out entirely (Requirement 1.8); an empty detections map merges an empty
list (Requirement 1.5).

**Why the capture record and not tags**: `parse_msg` only surfaces
`is_anomalous`/`confidence` because those are the only tags `eminfer`
posts; adding tags means changing the proprietary plugin. The marshal
record already exists, is repo-owned, carries source-pixel boxes, and one
single-frame run writes exactly one record — no correlation ambiguity.

**Sort orders** (`Detection_Sort_Order`), computed on box centers:
- `left_to_right` (default): center-x ascending, ties center-y ascending
- `right_to_left`: center-x descending, ties center-y ascending
- `top_to_bottom`: center-y ascending, ties center-x ascending
- `bottom_to_top`: center-y descending, ties center-x ascending
- `confidence_desc`: confidence descending, ties by `left_to_right`

The sort order lives on the `model_inference` node, but `model_inference`
compiles to GStreamer elements (no executor binding), so the compiled
document does not carry its parameters. Rather than adding a compiler pass,
the executor reads the parameter from the registration artifact's
`workflow.json` via the existing `run_artifacts.read_workflow_graph`
(Requirement 6.6 posture: descriptor-only catalog change).

### Custom Python delivery (Decision 1b, Requirement 1.10)

Custom Python bridges pump frames **during** the pipeline, but the capture
record is written by the marshal when `emltriton` processes the buffer —
**before** that same buffer reaches a downstream appsink. The bridge pump
(`python_bridge.py`, executor process) therefore injects detections into
the handler metadata with a bounded poll:

- At compile-topology level, the executor knows which `custom_python` /
  `custom_python_preprocess` nodes are stream-downstream of a
  `model_inference` node (segment element order).
- For those nodes only, the pump polls `read_detections(...)` (up to
  `DETECTIONS_POLL_BUDGET_SEC = 2.0`, 50 ms interval) before dispatching
  the frame, and sets `metadata["detections"]` / `metadata["detection_count"]`
  (IDs included) when found. On budget exhaustion the keys are absent and a
  warning lands in the run log — never a failure.
- Nodes not downstream of `model_inference` see byte-identical metadata.

The per-run Detection_List is built exactly once (same sort, same IDs): the
pump's poll caches the built list on the executor's run state, and the
post-pipeline merge reuses the cached list when present, so a custom node
and the Bedrock processor always see identical Detection_IDs.

### Detection crops (Decision 2, Requirement 2)

In `BedrockInferenceProcessor._run_one`, when the binding carries
`crop_detection_index`:

1. Resolve entry k from `tag_values["detections"]` (the processor gains
   access to the run metadata it already merges into). Missing list or
   index out of range -> **recorded error outcome** (see Error Handling),
   not a raise.
2. Load the captured `in` JPEG (`cv2.imread`), map the source-pixel box to
   the captured frame. The capture sink chain
   (`videoconvert ! jpegenc ! multifilesink`) performs no scaling, so
   captured dimensions equal source dimensions; the mapping degenerates to
   a clamp. **Defensively**, when dimensions differ the box is scaled by
   `captured_w / source_w` (source dimensions taken from the capture
   record's input image) — this also keeps crops correct if a resize
   element ever precedes the sink.
3. Expand by `crop_margin_percent` per side (percentage of the box's own
   width/height), clamp to frame bounds, slice, re-encode JPEG (quality 95).
4. Persist as `{output_dir}/{capture_id}.crop.{detection_id}.jpg`
   (Requirement 2.5) and send the crop bytes as the "Input image" content
   block. Record `bedrock.{nodeId}.detection_id`.

`cv2` and `numpy` are existing backend-container dependencies; no new
packages.

### Payload references (Decision 3, Requirement 3)

New module `src/backend/workflow_engine/payload_fetch.py` (shared, small,
no `python_bridge` import — that module's helpers live in a subprocess
source string):

```python
def resolve_payload_path(payload_json, dotted_path) -> Any
    # "refs.0.image": dict keys and integer list indices.

def fetch_reference_bytes(value, allowed_prefixes) -> bytes
    # s3:// -> boto3 get_object (streamed, size-capped)
    # http(s):// -> urllib with REFERENCE_FETCH_TIMEOUT_SEC = 10
    # "data:image/...;base64,..." or bare base64 -> base64.b64decode
    # Enforces MAX_REFERENCE_BYTES = 8 MiB and the prefix allow-list
    # (prefix check applies to URI fetches; base64 needs no gate).
    # Validates the result decodes as an image (cv2.imdecode) before use.
```

In `_run_one`, `reference_payload_path` takes the place of the captured
reference frame: resolved -> fetched/decoded -> appended as the
"Reference image" content block. Any failure is a **recorded error
outcome** for the node — never the single-image fallback the frame-port
path uses (Requirement 3.5: a payload-configured reference is an explicit
contract; silently inspecting without it would produce a false verdict).
The run log records the resolved source string, never image bytes
(Requirement 3.8). The `boto3` client uses the same default credential
chain as the Bedrock client (Greengrass TES on device).

Validator: a node with both `reference_payload_path` and a fed `reference`
port is a configuration error (new check, see Validator section).

### Verdict namespacing (Decision 4, Requirement 4)

`_run_one`'s anomaly-mode return gains nested keys, reusing the existing
nested-`bedrock` merge in `process` (freeform already merges
`bedrock.{nodeId}.text` per node):

```python
verdict["bedrock"] = {node_id: {
    "is_anomalous": ..., "confidence": ..., "text": answer,
    # when a Detection_Crop was inspected:
    "detection_id": ...,
}}
```

Recorded error outcomes land at `bedrock.{nodeId}.error`. Flat
`is_anomalous`/`confidence` keep last-writer-wins exactly as today.
`render_template` and `evaluate_condition` already resolve dotted keys
against nested dicts (the mechanism llm's `llm.{nodeId}.generated_text`
uses), so Requirements 4.3/4.4 need no template-engine change — covered by
tests, not code.

### Concurrency and publish-on-completion (Decision 5, Requirement 5)

**Branch derivation.** A *Bedrock branch* is the set of executor bindings
whose transitive `upstreamNodeIds` closure (over the document's
`executorBindings`) reaches exactly one `bedrock_inference` node and no
`llm_inference` node. Bindings reaching zero Bedrock nodes, multiple
Bedrock nodes, or any LLM node are *non-branch* and keep today's post-run
ordering byte-identical (Requirement 5.7). Derived once per run in a new
`branching.py` helper:

```python
def bedrock_branches(document) -> Dict[node_id, BranchPlan]
    # BranchPlan: the bedrock binding + its branch-scoped output/gate
    # binding ids (inference_filter/conditional/metadata bindings feed
    # gating and are evaluated per-branch, not "run").
```

**Execution.** `BedrockInferenceProcessor.process` gains a
`ThreadPoolExecutor(max_workers=min(len(bindings), 4))`:

- Each worker runs `_run_one`, then under a shared `threading.Lock`:
  merges the outcome (nested + flat keys; flat writes keep dict-update
  semantics — last *completion* wins, which is the concurrent analogue of
  today's last-binding-wins and remains unspecified order by design), then
  snapshots the metadata and invokes
  `OutputBindingProcessor.process_subset(document, snapshot, branch_ids)`
  for its branch — a new method that runs only the named bindings through
  the existing gating/condition/template code paths (same code, filtered
  binding list). MQTT/OPC UA/modbus runners construct per-call clients
  already, so branch-parallel publishes need no client changes.
- A branch whose Bedrock outcome is a recorded error skips its output
  bindings except: the error is set on the node status (gating semantics
  identical to a failed condition — Requirement 2.4/3.5), and other
  branches proceed (Requirement 5.4/5.5).
- `process` returns after **all** futures complete (join), with every
  outcome merged — the LLM processor, the non-branch output bindings, and
  finalization then run exactly as today (Requirement 5.6). A legacy-path
  `BedrockInferenceError` (no new parameters on that binding) still fails
  the run after the join, preserving Requirement 7.1.

**Thread safety inventory**: metadata dict (lock-guarded merges +
per-branch snapshots), `NodeStatusCollector` (its setters gain an internal
lock; single-writer accesses today make this additive),
`duration_sink`/`detail_sink` (routed through the same lock). The Bedrock
`boto3` client is created per invocation already (lazy import in the
invoker) — no sharing.

**Why threads, not async**: the processor is already called on a worker
thread; binding work is network-I/O-bound (Bedrock Converse, S3 fetch,
MQTT publish) with 30 s read timeouts; a small pool bounds memory and
avoids event-loop plumbing through synchronous boto3/paho code.

### Catalog, validator, designer (Requirement 6)

**`workflow_core/catalog/nodes.py`** (Portal layer + vendored mirror,
byte-identical):
- `MODEL_INFERENCE.parameters` += `detection_sort_order` (enum, default
  `left_to_right`, values as in the glossary; not referenced by any
  element chain — deliberately executor-read from `workflow.json`).
- `BEDROCK_INFERENCE.parameters` += `crop_detection_index` (int, optional,
  `min: 0`), `crop_margin_percent` (int, optional, default 0,
  `min: 0, max: 100`), `reference_payload_path` (string, optional),
  `allowed_uri_prefixes` (string, optional, mirroring
  `custom_python_source`'s wording). Descriptions and examples document
  the Detection_Sort_Order dependence and the payload path syntax.

The designer's NodeConfigPanel renders parameters from descriptors; no
frontend code change. The compiler copies node parameters into executor
bindings untouched, so the Bedrock_Binding carries the new parameters with
no compiler change; `detection_sort_order` intentionally never reaches the
compiled document (executor reads the graph).

**Validator** (`workflow_core/validator/checks.py`), new checks:
- `CODE_BEDROCK_REFERENCE_CONFLICT` (error): `reference_payload_path` set
  AND the node's `reference` port fed (Requirement 3.6).
- `CODE_BEDROCK_CROP_NO_MODEL` (warning): `crop_detection_index` set in a
  graph with no `model_inference` node (Requirement 6.2).
- `CODE_BEDROCK_PAYLOAD_NO_TRIGGER` (warning): `reference_payload_path`
  set in a graph with no CATEGORY_TRIGGER node (Requirement 6.3).
- Range violations (negative index, margin outside 0-100) are already
  errors via the descriptor constraint mechanism (Requirement 6.4).

### Simulation (Requirement 7)

Untouched. The sim mappings stub `bedrock_inference`/`model_inference`
already; new parameters ride the descriptors without sim-path meaning, and
simulated runs inject outcomes as today.

## Components and Interfaces

| Component | File | Change |
|---|---|---|
| Detections reader | `src/backend/workflow_engine/detections.py` (NEW) | `read_detections`, `build_detection_list`, `resolve_sort_order`, `merge_detections(tag_values, ...)` |
| Payload fetch | `src/backend/workflow_engine/payload_fetch.py` (NEW) | `resolve_payload_path`, `fetch_reference_bytes` (timeout, size cap, prefix gate, image validation) |
| Branch planner | `src/backend/workflow_engine/branching.py` (NEW) | `bedrock_branches(document) -> Dict[node_id, BranchPlan]` |
| Bedrock processor | `src/backend/workflow_engine/output_bindings.py` | `_run_one` gains crop + payload-reference resolution and nested verdict keys; `process` gains the thread pool, lock-guarded merge, per-branch completion callback invoking `process_subset`; signature extended with `run_context` (metadata access, output_dir/capture_id, graph document, node-status collector) — default None keeps every existing caller/test byte-identical |
| Output processor | `src/backend/workflow_engine/output_bindings.py` | NEW `OutputBindingProcessor.process_subset(document, metadata, binding_ids, ...)` — the existing `process` body refactored to accept a binding filter; `process()` delegates with "all non-branch bindings" |
| Executor | `src/backend/workflow_engine/pipeline_executor.py` | Detections merge call site (post `_repair_capture_artifacts`); passes `run_context` to the Bedrock processor; excludes branch-scoped bindings from the post-run handler |
| Bridge pump | `src/backend/workflow_engine/python_bridge.py` | Detections poll + metadata injection for custom nodes stream-downstream of `model_inference` |
| Node status | `src/backend/workflow_engine/node_status.py` | Internal lock on collector setters (additive thread safety) |
| Catalog | `edge-cv-portal/backend/layers/workflow_core/catalog/nodes.py` + vendored mirror | `detection_sort_order` on MODEL_INFERENCE; 4 new BEDROCK_INFERENCE parameters |
| Validator | `workflow_core/validator/checks.py` (both copies) | `CODE_BEDROCK_REFERENCE_CONFLICT` (error), `CODE_BEDROCK_CROP_NO_MODEL` (warning), `CODE_BEDROCK_PAYLOAD_NO_TRIGGER` (warning) |

## Correctness Properties

### Property 1: ID stability within a run

Every consumer of the run's Detection_List (bridge pump, Bedrock crops, persisted metadata) sees the same entries with the same Detection_IDs — the list is built once and cached on the run state.

**Validates: Requirements 1.3, 1.10, 2.7**

### Property 2: Order/ID independence

Detection_IDs are drawn from `uuid4`, never from list position; re-sorting the same raw detections changes entry order but never re-labels an entry.

**Validates: Requirements 1.3, 1.4**

### Property 3: Crop containment

Every Detection_Crop rectangle, after margin expansion, is fully contained in the captured frame bounds and non-empty (a degenerate box yields a recorded error, not a zero-size crop).

**Validates: Requirements 2.2, 2.3**

### Property 4: No-fallback invariant

A Bedrock_Binding configured with `reference_payload_path` either sends exactly two images (crop/frame + payload reference) or records an error — it never silently sends one.

**Validates: Requirements 3.5**

### Property 5: Branch isolation

A recorded error in branch B gates only B's downstream bindings; the set of bindings run for every other branch is identical to the all-success case.

**Validates: Requirements 2.4, 3.5, 5.4, 5.5**

### Property 6: Join completeness

The run's terminal status is decided only after every Bedrock future has completed and merged; persisted Run_Metadata contains every branch's nested outcome (verdict or error).

**Validates: Requirements 5.6**

### Property 7: Additive-only compatibility

With no new parameters configured, the bytes sent to Bedrock, the flat metadata keys, and the output-binding execution order are byte/order-identical to the pre-feature engine (pinned by regression tests).

**Validates: Requirements 7.1, 7.3**

### Property 8: Non-branch ordering

Bindings not in any Bedrock branch execute in the same order and with the same effective metadata as today, regardless of branch concurrency.

**Validates: Requirements 5.7**

## Data Models

**Detection_List entry** (in `tag_values["detections"]`, persisted in
`{capture_id}.json`):

```json
{
  "id": "3f9a2c1e",
  "label": "blue box",
  "confidence": 0.42,
  "x_min": 23.9, "y_min": 2793.6, "x_max": 869.5, "y_max": 2949.1
}
```

Coordinates are source-frame pixels (floats, as the marshal emits them).
`detection_count` is `len(detections)`. Nested verdict namespace after a
run with crops:

```json
"bedrock": {
  "bedrock_1": {"is_anomalous": false, "confidence": 0.93,
                 "text": "...", "detection_id": "3f9a2c1e"},
  "bedrock_2": {"error": "crop_detection_index 1 requested but only 1
                 detection(s) available"}
}
```

**MES payload contract (documentation, not validation)** — the workflow
author addresses it with dotted paths; the engine imposes no schema:

```json
{"refs": [{"id": "plate-A", "image": "s3://bucket/refA.jpg"},
           {"id": "plate-B", "image": "data:image/jpeg;base64,..."}]}
```

## Error Handling

| Failure | Behavior |
|---|---|
| No capture record / no detections block | `detections` key absent; run proceeds (R1.8) |
| Zero detections reported | `detections: []`, `detection_count: 0` (R1.5) |
| `crop_detection_index` >= detection count, or no Detection_List | Recorded error at `bedrock.{nodeId}.error` naming node, index, count; branch outputs gated; run and other branches proceed (R2.4) |
| Payload path unresolvable / fetch failure / not an image / prefix denied / size cap | Recorded error, branch gated; **no** single-image fallback (R3.5) |
| Reference fetch slow | 10 s timeout -> recorded error (R3.7) |
| Legacy binding failure (no new params) | `BedrockInferenceError` fails the run after the join — today's semantics (R7.1) |
| Branch output binding raises | Collected per branch; surfaced through the existing `OutputBindingError` aggregation after the join; other branches unaffected |
| Detections poll budget exhausted (custom node) | Metadata keys absent; warning in run log; never a failure (R1.10 degraded) |

## Testing Strategy

Host-runnable unit tests (no device, no network — fakes/injected invokers,
matching the existing `test/backend-test/workflow_engine/` patterns):

1. `detections.py`: record parsing (real jsonl fixture from thor1), sort
   orders incl. tie-breaks, ID uniqueness/randomness, empty map -> `[]`,
   absent block -> None, sort-order resolution from a workflow.json fixture.
2. Crop math: margin expansion, clamping, dimension-mismatch scaling,
   artifact naming with Detection_ID.
3. `payload_fetch.py`: dotted-path resolution (dict/list mix), base64 and
   `data:` URL decode, prefix gating, size cap, timeout wiring, non-image
   rejection.
4. Bedrock processor: crop-index error outcomes, payload-reference error
   outcomes (no fallback), nested verdict merge, flat-key compatibility,
   byte-identical request content when no new params (R7.1 regression
   pin), `detection_id` recording.
5. Concurrency: deterministic fakes with controlled completion order —
   publish-on-completion ordering, error isolation between branches,
   join-before-LLM/non-branch bindings, branch derivation (single/multi
   bedrock, llm exclusion, conditional gating inside a branch).
6. Validator: the three new checks + range constraints.
7. Catalog: Portal/vendored mirror byte-equality (existing test pattern).

**Device verification (USER ACTION, per the builds steering rule)**: the
end-to-end flow — MQTT trigger with a 3-reference payload, live Basler
frame, yolo-world detections, 3 concurrent crops + Bedrock verdicts, 3
Greengrass publishes — must be exercised on jetson-thor1 (JP7) from a real
built+deployed component before the feature is called done, including
sustained backend health.

## Risks and Open Questions

1. **Capture-record presence without a `capture` node.** The detections
   mechanism assumes every `model_inference` run lands `{capture_id}.jsonl`
   through the em-agent broker routing. Verified paths suggest yes (the
   executor configures file-target routing per run), but this must be
   confirmed on-device early (first implementation task). Fallback if it
   does not hold: the Marshal_Model (repo-owned) writes a detections
   sidecar JSON keyed by capture id — same reader interface, marshal-only
   change.
2. **Record write timing for the custom-node poll.** The marshal writes
   during buffer processing but the broker file write may be async; the
   2 s poll budget covers it, and the degraded path is documented.
3. **Concurrent same-model runs.** Two simultaneous runs of workflows
   sharing one model each read their own `{output_dir}/{capture_id}`
   record, so there is no cross-run ambiguity by construction.
4. **Flat-key ordering under concurrency.** Last-completion-wins replaces
   last-binding-wins for the flat keys. Both are unspecified orders;
   consumers needing determinism use the nested keys (that is the point of
   Requirement 4).
