# Design Document

## Overview

`OutputBindingProcessor.process()` gains an optional, injectable `detail_sink: Callable[[str, str], None]` (node_id, detail). The pipeline executor passes a sink that writes into the run's `NodeStatusCollector` (a new contained `set_detail(node_id, detail)` method that records a detail WITHOUT changing status and never overwrites an existing failure detail). Each binding runner composes its sent-message summary after a successful send; the gating paths compose skipped outcomes. No API or frontend change: the details ride the existing `node_status_json` → `GET /workflows/executions/{id}/node-status` → node-click flow.

## Key design points

1. **`NodeStatusCollector.set_detail(node_id, detail)`** (new): records `detail` for a tracked node; no-op for untracked/None nodes; NEVER overwrites a detail belonging to a `failure` status (3.3); fully contained (R8.5 discipline).
2. **`OutputBindingProcessor(..., detail_sink=None)`**: default None → behavior byte-identical to today (portal/test callers unaffected). The executor wires `detail_sink=collector.set_detail` when it constructs/invokes the post-run handler. Since `OutputBindingProcessor` is constructed once at executor setup but the collector is per-run, the sink is threaded through `process(registration, document, tag_values, detail_sink=None)` — via the executor's post-run handler call site (`__call__` passes it through). Confirm the exact call-site shape in pipeline_executor before implementing; keep the PostRunHandler signature backward compatible (extra optional kwarg or a contextvar — prefer the explicit optional kwarg with the executor updated to pass it).
3. **Detail composition** per runner (bounded: payload/value repr truncated to 512 chars with '…'):
   - mqtt: `sent to topic '<topic>' (qos <q>, <plain|aws_iot|greengrass>): <payload>`
   - opcua: `wrote <value!r> to <node_id> at <endpoint>`
   - dio: `set pin <pin> <signal_type>` / `pulsed pin <pin> (<width>ms)`
   - gated: `not sent: gated out by an upstream inference filter or conditional`
   - condition false/unevaluable: `not sent: condition <condition!r> evaluated false` / `not sent: condition <condition!r> could not be evaluated`
4. **Failure precedence**: runners record the sent detail ONLY on success (after the runner returns); the except path records nothing new (mark_failure already captures the error, and set_detail refuses to overwrite failure details).
5. **Truncation helper**: module-level `_preview(text, limit=512)`.

## Correctness Properties

Property 1: Successful sends record bounded sent-message details

_For any_ document with mqtt/opcua/dio bindings and any metadata, when a binding's runner succeeds, the detail sink receives exactly one detail for that node containing the identifying fields (topic/qos for mqtt, node id+endpoint for opcua, pin/signal for dio) and the rendered payload/value truncated to the bound; gated/skipped bindings receive their skipped-outcome detail; the recorded map surfaces through NodeStatusCollector.to_map() under detail.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 2.1, 2.3**

Property 2: Preservation - behavior identical apart from detail recording

_For any_ inputs: with detail_sink=None, process() behavior is byte-identical to today (same client calls, same OutputBindingError aggregation); with a sink, the client calls and error aggregation are STILL identical (sink is called after/around, never alters flow, and a raising sink is contained); failure details always win over sent details for the same node; non-output bindings are untouched.

**Validates: Requirements 1.5, 3.1, 3.2, 3.3, 3.4**

## Testing Strategy

- Unit tests (new file `test/backend-test/workflow_engine/test_output_sent_message_details.py`): injectable clients + recording sink; Hypothesis over payload templates/metadata for the mqtt/opcua composition and truncation bound; gated/condition-false/unevaluable outcomes; raising sink contained; failure precedence (failing runner → no sent detail, error detail intact); detail_sink=None baseline equality.
- NodeStatusCollector.set_detail tests in the existing node_status test file's style: untracked node no-op, failure-detail precedence, containment.
- Executor integration: extend the existing executor harness test to assert node_status_json carries the mqtt sent detail after a run with a stubbed publisher.
- Device-side only; rides the NEXT LocalServer build. No portal change.
