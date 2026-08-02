# Bugfix Requirements Document

## Introduction

This bugfix collects five independent defects and gaps found while integration-testing
the Workflow Manager (the drag-and-drop workflow designer in
`edge-cv-portal/frontend/src/pages/workflows` plus its node catalog source of truth in
`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`, and the
edge workflow engine in `src/backend/workflow_engine`). Each issue is treated as its own
bug condition so it can be fixed and verified in isolation:

1. **VLM/LLM inference input type** — the `llm_inference` node declares an `InferenceMeta`
   input port, so it consumes inference metadata rather than video frames. As a
   vision-language node it should take video frames as input.
2. **MQTT publish via Greengrass** — the `mqtt_publish` node can only target a plain MQTT
   broker (required `broker_host`) or AWS IoT Core over mutual TLS (certificate file paths).
   There is no zero-configuration path that publishes through the on-device Greengrass IPC so
   the user only has to supply the topic.
3. **Model inference fan-out** — the designer does not let a model inference node's single
   output port connect to more than one downstream node (for example fanning out to a
   conditional and to another output at the same time), even though the compiler already
   realizes fan-out with `tee`/`queue`.
4. **Node label** — the `llm_inference` node is labelled "LLM Inference" in the palette and on
   the canvas; it should read "VLM/LLM Inference".
5. **Workflow name not shown** — while viewing or editing a workflow, the builder does not
   display the workflow's name anywhere at the top of the screen, so the user cannot tell which
   workflow is open.

The five issues share no code paths and can be fixed independently. This document captures the
defective behavior, the corrected behavior, and the behavior that must be preserved for each.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the `llm_inference` node type is declared in the node catalog THEN the system declares its input port as `InferenceMeta` (`PortDescriptor("in", PORT_TYPE_INFERENCE_META)`), so the node consumes upstream inference metadata instead of video frames and a video-frame source cannot connect directly into it.

1.2 WHEN a user configures the `mqtt_publish` node to publish a message THEN the system requires the user to supply a `broker_host` (or enable `aws_iot` and provide IoT thing name and certificate file paths); there is no option that publishes through the device's Greengrass-managed MQTT so that only the topic must be configured.

1.3 WHEN a user draws a second outgoing connection from a model inference node's output port (a node in the `inference` category — Model Inference, Bedrock Inference, or LLM Inference) to a second downstream node, for example to both a conditional and another output node, THEN the designer does not create the second connection, so the model inference output cannot fan out to multiple downstream nodes.

1.4 WHEN the `llm_inference` node appears in the Node_Palette and on the canvas THEN the system displays its label as "LLM Inference".

1.5 WHEN a user opens or edits a saved workflow in the Workflow_Builder THEN the system does not display the workflow's name at the top of the screen, so the user cannot tell from the view which workflow is currently open.

### Expected Behavior (Correct)

2.1 WHEN the `llm_inference` node type is declared in the node catalog THEN the system SHALL declare its input port as `VideoFrames` (`PORT_TYPE_VIDEO_FRAMES`) so the node takes video frames as input and a video-frame source can connect directly into it.

2.2 WHEN a user configures the `mqtt_publish` node THEN the system SHALL offer a Greengrass publishing option that publishes through the device's Greengrass-managed MQTT, requiring only the topic to be configured (no broker host, port, or certificate paths), while still validating and packaging as a valid output node.

2.3 WHEN a user draws a second (or further) outgoing connection from a model inference node's output port to another downstream node THEN the designer SHALL create the connection, so a model inference node output SHALL support fan-out to multiple downstream nodes (subject to the existing port-type compatibility rules).

2.4 WHEN the `llm_inference` node appears in the Node_Palette and on the canvas THEN the system SHALL display its label as "VLM/LLM Inference".

2.5 WHEN a user opens or edits a saved workflow in the Workflow_Builder THEN the system SHALL display the workflow's name at the top of the screen so the user can identify which workflow is open.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the `llm_inference` node emits its result THEN the system SHALL CONTINUE TO produce `InferenceMeta` on its output port and SHALL CONTINUE TO expose its existing parameters (`modelName`, `prompt_template`, `max_tokens`, `temperature`, `top_p`) and its existing architecture mappings (vLLM-capable device archs plus the `sim` stub) unchanged.

3.2 WHEN a user uses the existing `mqtt_publish` broker or AWS IoT Core (`aws_iot`) publishing paths THEN the system SHALL CONTINUE TO accept and validate those configurations, package the paho-mqtt dependency, and publish exactly as before; the new Greengrass option SHALL be additive and off by default.

3.3 WHEN a user draws a connection between two ports with incompatible declared types, connects a node to itself, or uses an unknown port handle THEN the designer SHALL CONTINUE TO reject the connection with the existing reason message; fan-out SHALL NOT relax any port-type compatibility, cycle, or self-connection rule.

3.4 WHEN a single model inference node output has exactly one downstream connection, or when any other node type is connected THEN the system SHALL CONTINUE TO behave exactly as before, and the compiled pipeline SHALL CONTINUE TO reference every node exactly once with fan-out realized via `tee`/`queue`.

3.5 WHEN node types other than `llm_inference` are shown in the palette or on the canvas THEN the system SHALL CONTINUE TO display their existing labels unchanged, and the `llm_inference` node's `type_id` SHALL remain `llm_inference` (only the display label changes).

3.6 WHEN a workflow is unsaved/new (has no name yet), and for all existing toolbar actions (New, Open, Save, Validate, Duplicate, Delete, Package, Generate, Test) THEN the Workflow_Builder SHALL CONTINUE TO function exactly as before; showing the name SHALL be display-only and SHALL NOT change save, load, or validation behavior.

## Bug Condition and Property Specification

### Bug 1 — VLM/LLM inference should take video frames

```pascal
FUNCTION isBugCondition1(X)
  INPUT: X of type NodeTypeDescriptor
  OUTPUT: boolean

  RETURN X.type_id = "llm_inference"
         AND inputPortType(X, "in") = PORT_TYPE_INFERENCE_META
END FUNCTION
```

```pascal
// Property: Fix Checking
FOR ALL X WHERE isBugCondition1(X) DO
  descriptor' ← catalog'(X.type_id)
  ASSERT inputPortType(descriptor', "in") = PORT_TYPE_VIDEO_FRAMES
  ASSERT outputPortType(descriptor', "out") = PORT_TYPE_INFERENCE_META  // unchanged
END FOR
```

### Bug 2 — MQTT publish through Greengrass with only a topic

```pascal
FUNCTION isBugCondition2(X)
  INPUT: X of type MqttPublishConfig
  OUTPUT: boolean

  // The user wants Greengrass-managed publishing but the only ways to
  // produce a valid config force a broker host or AWS IoT certificate paths.
  RETURN wantsGreengrassManagedPublish(X)
         AND NOT existsValidConfig(topicOnly(X))
END FUNCTION
```

```pascal
// Property: Fix Checking
FOR ALL X WHERE isBugCondition2(X) DO
  cfg' ← greengrassPublish(topic = X.topic)   // only the topic supplied
  ASSERT isValidMqttPublishConfig'(cfg')
  ASSERT NOT requires(cfg', "broker_host")
  ASSERT NOT requires(cfg', "iot_ca_cert_path")
END FOR
```

### Bug 3 — Model inference output fan-out

```pascal
FUNCTION isBugCondition3(X)
  INPUT: X of type ConnectionAttempt
  OUTPUT: boolean

  // A second acceptable outgoing connection from a model inference
  // node's output port is not created by the designer.
  RETURN isModelInferenceNode(X.sourceNode)
         AND isOutputPort(X.sourceNode, X.sourceHandle)
         AND portsCompatible(X)
         AND outgoingCount(X.sourceNode, X.sourceHandle) >= 1
         AND NOT connectionCreated(X)
END FUNCTION
```

```pascal
// Property: Fix Checking
FOR ALL X WHERE isBugCondition3(X) DO
  graph' ← attemptConnect'(X)
  ASSERT connectionCreated'(X) = TRUE
  ASSERT outgoingCount'(graph', X.sourceNode, X.sourceHandle) = outgoingCount(...) + 1
END FOR
```

### Bug 4 — Node label reads "VLM/LLM Inference"

```pascal
FUNCTION isBugCondition4(X)
  INPUT: X of type NodeTypeDescriptor
  OUTPUT: boolean

  RETURN X.type_id = "llm_inference" AND X.display_name = "LLM Inference"
END FUNCTION
```

```pascal
// Property: Fix Checking
FOR ALL X WHERE isBugCondition4(X) DO
  descriptor' ← catalog'(X.type_id)
  ASSERT displayName(descriptor') = "VLM/LLM Inference"
  ASSERT typeId(descriptor') = "llm_inference"   // identifier unchanged
END FOR
```

### Bug 5 — Workflow name shown while editing

```pascal
FUNCTION isBugCondition5(X)
  INPUT: X of type BuilderView
  OUTPUT: boolean

  RETURN hasOpenWorkflow(X)
         AND NOT displaysWorkflowName(X)
END FUNCTION
```

```pascal
// Property: Fix Checking
FOR ALL X WHERE isBugCondition5(X) DO
  view' ← render'(X)
  ASSERT displaysWorkflowName(view') = TRUE
  ASSERT displayedName(view') = openWorkflowName(X)
END FOR
```

### Preservation (all five bugs)

```pascal
// Property: Preservation Checking
FOR ALL X WHERE NOT (isBugCondition1(X) OR isBugCondition2(X)
                     OR isBugCondition3(X) OR isBugCondition4(X)
                     OR isBugCondition5(X)) DO
  ASSERT F(X) = F'(X)
END FOR
```

**Key Definitions:**
- **F**: the workflow designer, node catalog, and workflow engine as they exist before the fix.
- **F'**: the same after applying the five fixes (VLM/LLM frame input, Greengrass MQTT option,
  model-inference output fan-out, "VLM/LLM Inference" label, and workflow-name display).
- **Counterexamples**: (1) an `llm_inference` node whose `in` port rejects a `VideoFrames`
  source; (2) an MQTT publish that cannot be configured with only a topic; (3) a model
  inference output that refuses a second downstream connection; (4) a palette entry reading
  "LLM Inference"; (5) an open workflow whose name is nowhere on screen.
