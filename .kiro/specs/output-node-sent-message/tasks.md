# Implementation Plan

## Overview

Records each output binding's sent message (or skipped reason) into the run's existing per-node status map so clicking an MQTT/OPC UA/DIO node in the run view shows what it sent. Feature spec: tests accompany implementation per task. Device-side only; rides the NEXT LocalServer build.

## Task Dependency Graph

```json
{
  "waves": [
    {"wave": 1, "tasks": ["1"], "description": "Collector set_detail + processor detail_sink + composition + tests."},
    {"wave": 2, "tasks": ["2"], "description": "Executor wiring + integration test + full suite checkpoint. Depends on wave 1."}
  ]
}
```

## Tasks

- [x] 1. Detail recording in the collector and output binding processor
  - `src/backend/workflow_engine/node_status.py`: add `set_detail(node_id, detail)` — records detail without changing status; no-op for untracked/None node; never overwrites a failure-status detail; fully contained (R8.5 style); surfaces via to_map()/to_json()
  - `src/backend/workflow_engine/output_bindings.py`: add `_preview(text, limit=512)` truncation helper; `OutputBindingProcessor.process()`/`__call__` gain optional `detail_sink` (default None → byte-identical behavior); compose details per design §3 (mqtt topic/qos/path + payload preview; opcua endpoint/node/value; dio pin/signal/pulse; gated and condition-false/unevaluable skipped outcomes); record sent details ONLY on runner success; a raising sink is contained (never affects binding execution or error aggregation)
  - New tests `test/backend-test/workflow_engine/test_output_sent_message_details.py` per design Testing Strategy (Property 1 + Property 2; Hypothesis over payload templates/metadata; truncation bound; failure precedence; sink containment; detail_sink=None baseline)
  - Run: `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/test_output_sent_message_details.py test/backend-test/workflow_engine/test_workflow_output_bindings.py -v` (locate the existing output-binding test file name(s) and include them) — green
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.3, 3.1, 3.2, 3.3_

- [x] 2. Executor wiring + checkpoint
  - `src/backend/workflow_engine/pipeline_executor.py`: thread the per-run collector's `set_detail` into the post-run handler call as the `detail_sink` (confirm the PostRunHandler call-site shape; keep backward compatibility — handlers that ignore the kwarg keep working; only pass it when the handler accepts it, e.g. inspect or try/except TypeError, mirroring existing patterns)
  - Extend the executor integration tests (existing harness): a run with a stubbed mqtt publisher finalizes with node_status_json carrying the sent detail for the mqtt node; a gated-out output node carries the skipped detail; a failing output node keeps its failure detail (no sent detail)
  - Checkpoint: `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/` — no new failures (stale-workflow-registrations exploration failures tolerated per steering if that fix hasn't landed)
  - _Requirements: 2.1, 2.2, 3.1, 3.4_

## Notes

- No portal/API/frontend change: `{nodeId: {status, detail?}}` shape is unchanged and the run view already renders `detail` on node click.
- Device-side change rides the NEXT LocalServer build together with bedrock-response-mode task 5.
- Known pre-existing failures per repo steering apply at the checkpoint.
