# Requirements Document

## Introduction

When a deployed workflow run completes, clicking an output node (mqtt_publish, opcua_write, digital_output) in the run-status graph shows only the node's status — there is no way to see what message the node actually sent (or why it didn't send one). This feature records each output binding's sent-message summary into the run's existing per-node status map (`node_status_json`, the `{nodeId: {status, detail?}}` map the run view already renders on node click), so the sent payload/value — and gated/skipped outcomes — are visible per node with no new API or frontend work.

## Glossary

- **Sent-message detail**: a human-readable summary recorded in the node's `detail` field — e.g. `sent to topic 'factory/line1' (qos 1): {"is_anomalous": true, ...}` for mqtt, `wrote <value> to ns=2;s=Tag1 at opc.tcp://...` for opcua, `pulsed pin 7 (100ms)` for digital_output.
- **Skipped outcome**: an output binding that deliberately did not fire — gated out by an upstream filter/conditional, or its own condition evaluated false — recorded as e.g. `not sent: gated out by upstream inference filter` / `not sent: condition 'is_anomalous == true' evaluated false`.
- **`OutputBindingProcessor`**: `src/backend/workflow_engine/output_bindings.py` post-run handler that evaluates gates and runs the mqtt/opcua/dio binding runners.
- **`NodeStatusCollector`**: `src/backend/workflow_engine/node_status.py` accumulator whose `{nodeId: {status, detail?}}` map persists to `WorkflowExecution.node_status_json` and is served by `GET /workflows/executions/{id}/node-status`.

## Requirements

### Requirement 1: Output bindings record what they sent

**User Story:** As a workflow operator, I want to click an MQTT or OPC UA node in the run view and see the exact message it sent, so I can verify downstream integrations without tailing broker/server logs.

#### Acceptance Criteria

1.1 WHEN an `mqtt_publish` binding publishes successfully THEN the system SHALL record a sent-message detail carrying the topic, qos, publish path (plain/aws_iot/greengrass), and the rendered payload (truncated to a bounded length with an ellipsis marker when long)

1.2 WHEN an `opcua_write` binding writes successfully THEN the system SHALL record a sent-message detail carrying the endpoint, node id, and the rendered value

1.3 WHEN a `digital_output` binding actuates successfully THEN the system SHALL record a sent-message detail carrying the pin, signal type, and pulse width where applicable

1.4 WHEN an output binding is gated out by an upstream inference filter/conditional, or its own condition evaluates false or is unevaluable THEN the system SHALL record a skipped outcome naming the reason

1.5 WHEN an output binding fails THEN the system SHALL CONTINUE TO record the failure detail exactly as today (the sent-message recording must not replace or obscure error details)

### Requirement 2: Details surface in the run view's existing node-click flow

#### Acceptance Criteria

2.1 WHEN the run finalizes THEN the recorded sent-message/skipped details SHALL be present in the persisted `node_status_json` map under each output node's `detail` field (successful sends keep status `success`; skipped outcomes keep the node's existing terminal status)

2.2 WHEN the frontend requests `GET /workflows/executions/{id}/node-status` THEN the details SHALL be returned with no API shape change (`{nodeId: {status, detail?}}` unchanged)

2.3 WHEN payloads contain long or sensitive-looking content THEN the system SHALL truncate the recorded payload to a bounded length (e.g. 512 chars) — the record is a preview, not an archive

### Requirement 3: Unchanged behavior

#### Acceptance Criteria

3.1 WHEN output bindings run THEN publishing/writing/actuation behavior, gating semantics, error containment (every binding attempted, OutputBindingError aggregation), and the run's final status SHALL CONTINUE TO be identical — this feature only adds detail recording

3.2 WHEN a run has no output bindings, or the detail recording itself errors THEN the system SHALL CONTINUE TO complete the run normally (recording is best-effort/contained, mirroring NodeStatusCollector's R8.5 discipline)

3.3 WHEN failure details are recorded (today's behavior) THEN they SHALL CONTINUE TO take precedence over sent-message details for the same node

3.4 WHEN non-output bindings (filters, conditionals, inference nodes) finalize THEN their status/detail behavior SHALL CONTINUE TO be unchanged
