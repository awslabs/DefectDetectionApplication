# Bugfix Requirements Document

## Introduction

A live JP6 device (LocalServer.arm64JP6 v1.0.45) running the llm_inference workflow `dda.workflow.1f0b4c0c-f5f0-430d-befe-a00aacc22c47` v2 (folder_source_1 → llm_inference_1 → capture_1 + mqtt_publish_1) exposed three related defects in the workflow output path during execution `85bf7a61-a126-484d-9074-08fbb73f209e` (2026-08-03 04:08):

**Defect A — Greengrass MQTT publish unauthorized.** The `mqtt_publish` output binding with `greengrass` enabled publishes through Greengrass IPC `PublishToIoTCore` (`src/backend/workflow_engine/output_bindings.py`, `_run_mqtt_publish` → `_default_greengrass_publisher`), but every LocalServer recipe variant's `aws.greengrass.ipc.mqttproxy` accessControl policy grants Publish/SubscribeToIoTCore only on shadow topics (`$aws/things/*/shadow/name/*`). Workflow topics are user-configured free strings (the node catalog's `topic` parameter accepts any non-empty string, e.g. `factory/line1/inspection`), so no policy covers them and the nucleus denies the publish with `awsiot.greengrasscoreipc.model.UnauthorizedError`, failing the binding and the run.

**Defect B — llm_inference 409 'loading' not handled; node stuck "pending"; output invisible.** The `Text_Generation_API` returned `409 {'model_name': 'opt125m-smoke', 'state': 'loading'}` — a transient model-warming state (`src/backend/vllm_runtime/server.py` maps non-READY state to 409 exactly so callers can distinguish loading from failed). The engine's `_default_llm_invoker` treats every non-200 as a terminal error with no retry, records `{'error': ...}` in in-memory metadata, and the run still completes as COMPLETED. The `NodeStatusCollector` is built only from the pipeline `element_name_map`, and `llm_inference` maps to an executor binding with no pipeline element, so llm_inference_1 never appears in `node_status_json` — the run-view graph resolves absent nodes to "pending" (`src/frontend/src/components/deployed-workflow/graph/graphGeometry.ts`). A successful llm_inference's generated text is merged only into in-memory tag values and is never persisted to the run directory or surfaced anywhere.

**Defect C — capture file named ".jpg" (empty basename) and no metadata JSON.** The run directory `/aws_dda/captures/1f0b4c0c-.../14f0b38b-.../` holds only `.jpg` (679428 bytes) and `run.log`. The message broker writes `file-target_{DIR}-{ext}` messages to `{DIR}/{c_id}.{ext}` where `c_id` is the buffer's correlation id (`src/backend/dda_triton/message_broker_client.py`). The correlation id is attached to GStreamer buffers only by `emltriton` (via `_inject_inference_metadata`); this workflow's pipeline is tritonless (folder_source compiles to plain `filesrc`; llm_inference is an executor binding, not an element), so `emlcapture` finds no correlation id (`emlcapture.cpp` defaults `id = ""`) and the broker writes `"" + ".jpg"` = `.jpg`. The `meta=` routing is likewise empty (`_route_capture_outputs` emits meta targets only for declared emltriton outputs), so no metadata JSON is ever written.

All three fixes ship in the LocalServer component (recipes + `src/backend`), so on-hardware JP6 verification requires a gdk build and is user-gated.

## Bug Analysis

### Current Behavior (Defect)

**Defect A — Greengrass MQTT publish unauthorized**

1.1 WHEN a workflow's `mqtt_publish` output binding with `greengrass` enabled publishes to a user-configured workflow topic THEN the system fails the binding with `UnauthorizedError` raised by the Greengrass IPC `PublishToIoTCore` operation, because no accessControl policy authorizes the topic

1.2 WHEN the LocalServer recipe variants grant `aws.greengrass.ipc.mqttproxy` access THEN the system authorizes Publish/SubscribeToIoTCore only on `$aws/things/*/shadow/name/*` shadow topics, while the mqtt_publish node's `topic` parameter accepts any non-empty string

1.3 WHEN the Greengrass publish is denied THEN the system surfaces only the bare exception (`UnauthorizedError`) in the run error, with no indication that the LocalServer component's accessControl configuration is the cause or how to remediate it

**Defect B — llm_inference 409 'loading' handling, node status, and output visibility**

1.4 WHEN the Text_Generation_API returns 409 with state `loading` (the model is warming) THEN the system fails the llm_inference node immediately with a terminal error instead of waiting for the model to become READY

1.5 WHEN an llm_inference binding fails THEN the system records the failure only in in-memory metadata and the run log, completes the run as COMPLETED, and persists no failure in `node_status_json`

1.6 WHEN the run-status graph renders an llm_inference node (or any executor-binding-only node that did not fail) THEN the system shows it as "pending" forever, because executor-binding nodes are absent from the element-name map the `NodeStatusCollector` is built from, and the frontend resolves nodes absent from `node_status_json` to "pending"

1.7 WHEN an llm_inference binding succeeds THEN the system keeps the generated text only in in-memory tag values (and a `completed; tags:` run-log line), persisting nothing to the run's artifact directory and surfacing the output in no view

**Defect C — capture artifact naming and metadata**

1.8 WHEN a capture node terminates a pipeline containing no `emltriton` element (a tritonless workflow such as folder_source → llm_inference → capture) THEN the system writes the captured frame as literally `.jpg` (empty basename), because no element attaches a buffer correlation id and the message broker names the file `{c_id}.{ext}` with `c_id` empty

1.9 WHEN capture output routing computes `emlcapture` meta targets for a tritonless document THEN the system emits `meta=""` (no declared Triton outputs) and writes no metadata JSON to the run directory, so the run's metadata (including any llm_inference output) has no on-disk destination

### Expected Behavior (Correct)

**Defect A — Greengrass MQTT publish authorized and diagnosable**

2.1 WHEN a workflow's `mqtt_publish` output binding with `greengrass` enabled publishes to any user-configured workflow topic THEN the system SHALL authorize the IPC `PublishToIoTCore` request (via an accessControl policy entry in every recipe variant covering workflow publish topics) and deliver the publish

2.2 WHEN a Greengrass IPC publish is denied with `UnauthorizedError` THEN the system SHALL surface an actionable error naming the LocalServer component's `aws.greengrass.ipc.mqttproxy` accessControl configuration as the cause and the topic that was denied

**Defect B — llm_inference transient handling, truthful status, visible output**

2.3 WHEN the Text_Generation_API returns 409 with state `loading` THEN the system SHALL retry the generate call within a bounded wall-clock budget until the model becomes READY, and proceed with the generated text when a retry succeeds

2.4 WHEN the 409 retry budget is exhausted, or the 409 state is `failed`/`unknown` (not `loading`) THEN the system SHALL record the node as failed with the API's state information in the error detail

2.5 WHEN an llm_inference binding fails (unresolved placeholder, API error, or exhausted loading budget) THEN the system SHALL persist that node's status as `failure` with its error detail in `node_status_json`, so the run view shows the failure instead of "pending"

2.6 WHEN an llm_inference binding succeeds THEN the system SHALL persist that node's status as `success`, and executor-binding nodes generally SHALL reach a terminal status in `node_status_json` (never remaining absent/"pending" after the run ends)

2.7 WHEN an llm_inference binding produces generated text THEN the system SHALL persist the output into the run's artifact directory (the same per-run directory that holds the captured image and `run.log`) so the user can find where the output went

**Defect C — meaningful capture filenames and metadata JSON**

2.8 WHEN a tritonless capture run writes its base frame THEN the system SHALL ensure the file has a meaningful basename (`{capture_id}.jpg`, where `capture_id` is the run's `{workflow_id}-{execution_id}`), consistent with what `run_artifacts` resolves for Triton runs

2.9 WHEN a workflow run with a per-run artifact directory completes post-run processing THEN the system SHALL write a metadata JSON file into the run directory carrying the run's inference metadata (including the `llm` results section when present)

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the LocalServer backend uses shadow pub/sub (StreamShadow/AppRunnerShadow via ShadowManager and the existing mqttproxy shadow-topic policy) THEN the system SHALL CONTINUE TO access shadow topics exactly as before

3.2 WHEN a `mqtt_publish` binding uses the plain-broker path (`broker_host`) or the `aws_iot` mutual-TLS path THEN the system SHALL CONTINUE TO publish through paho with exactly the same call arguments, port/QoS defaulting, and error behavior

3.3 WHEN the Text_Generation_API returns 200 on the first attempt THEN the system SHALL CONTINUE TO invoke the API once with the same URL, body (prompt + max_tokens/temperature/top_p), and timeout, and merge `{'generated_text': ...}` under `metadata['llm'][nodeId]` as today

3.4 WHEN an llm_inference prompt has an unresolved placeholder THEN the system SHALL CONTINUE TO record the node error without calling the API, and other bindings SHALL CONTINUE TO be processed independently (a binding failure never aborts the remaining bindings)

3.5 WHEN pipeline-element nodes report bus signals THEN the system SHALL CONTINUE TO map running/warning/success/failure statuses exactly as today (the collector's existing transitions and the failing-node mapping are unchanged)

3.6 WHEN a Triton capture workflow (with `emltriton`) runs THEN the system SHALL CONTINUE TO write `{capture_id}.jpg` plus the declared overlay/mask/jsonl targets via the existing correlation-id injection and meta routing, unchanged

3.7 WHEN the recipe variants are built and published THEN the system SHALL CONTINUE TO carry all existing accessControl policies (shadow, mqttproxy shadow entry, CLI), lifecycle scripts, and configuration unchanged apart from the added publish policy entry

3.8 WHEN the run-status graph renders pipeline-element nodes or a failed output-binding node THEN the system SHALL CONTINUE TO display their statuses exactly as today (the frontend needs no change; absent-node defaulting to "pending" remains for in-flight runs)
