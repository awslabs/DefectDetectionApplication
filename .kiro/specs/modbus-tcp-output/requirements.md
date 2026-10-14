# Requirements Document

## Introduction

This feature adds a **Modbus TCP output node** (`modbus_write`) to the workflow
builder: an OUTPUT-category node that, after a workflow run completes, writes a
value to a remote PLC over Modbus TCP — a coil or a holding register — exactly
the way `digital_output`, `mqtt_publish`, and `opcua_write` actuate today
(post-run executor binding, gated by upstream `conditional` / `inference_filter`
nodes).

The original request: *"connect to a remote PLC via modbus to trigger channels
of action based on output result behind a conditional."* Interpretation encoded
here (decisions noted):

- **One node writes one target** (a single coil or holding register). "Channels
  of action" are realized by wiring multiple `modbus_write` nodes behind
  different `conditional` branches — the same composition pattern
  `digital_output`/`opcua_write` use today. Single-target keeps the parameter
  surface, executor, and validation identical in shape to the existing output
  siblings; multi-channel-per-node was considered and rejected (it would need a
  list-typed parameter form no other node uses).
- **Node type id `modbus_write`** — symmetric with `opcua_write` and
  `digital_output`.
- **Write targets only**: coils and holding registers are writable in Modbus;
  discrete inputs and input registers are read-only and are excluded.
- **Output side only**: no trigger/subscribe side, no recipe or accessControl
  work (plain TCP from the LocalServer backend container; no Greengrass IPC
  involvement).

### Out of scope (deferred)

- A Modbus read/subscribe trigger node (would be a sibling of
  `mqtt_subscribe`/`opcua_subscribe`).
- Multi-register writes (Write Multiple Coils 0x0F / Write Multiple Registers
  0x10), 32-bit/float encodings spanning two registers, and Modbus RTU/serial.
- Modbus/TCP security (TLS) — plain TCP only, matching the plain-broker MQTT
  path.

### Grounded artifacts

- Catalog (both copies MUST stay byte-in-sync):
  `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/` (source
  of truth) and `src/backend/workflow_engine/vendor/workflow_core/` (device
  vendored copy).
- Device executor: `src/backend/workflow_engine/output_bindings.py`
  (`OutputBindingProcessor`, injectable client seams, conditional/filter
  gating, `render_template` value templating, `OutputBindingError`
  containment).
- Validator: `workflow_core/validator/checks.py` (generic `V4` required-param
  check covers this node; no new check code).
- Compiler: `workflow_core/compiler/compiler.py` (generic executor-binding
  emission; zero compiler changes expected).
- Frontend mirror: `edge-cv-portal/frontend/src/pages/workflows/` (`types.ts`,
  `NodeConfigPanel.tsx` `"name=value"` dependent gating, `inlineChecks.ts`).
- Simulation: `workflow_core.catalog.SIM_RECORDING_BINDING_PREFIX` recording
  stubs, executed prefix-generically by
  `edge-cv-portal/test-sandbox/harness/bindings.py`.
- Goldens/baselines: `layers/workflow_core/tests/catalog_baseline.json`,
  `layers/workflow_core/tests/golden_zero_trigger_compilation.json`.
- Dependency ground truth: `src/backend/requirements.txt` is
  preservation-gate-tracked (`test/backend-test/security/baselines/`,
  `.kiro/steering/builds.md`); `pymodbus` is NOT installed in the flask-app
  container today.

## Glossary

- **Node_Type_Catalog**: the data-only list of `NodeTypeDescriptor` records
  defining every workflow node type, existing in two byte-identical Python
  copies (portal layer and device vendored copy) plus a hand-maintained
  frontend mirror in `types.ts`.
- **Modbus_Write_Node**: the new `modbus_write` output node type
  (`CATEGORY_OUTPUT`): after a workflow run completes, writes one value to one
  coil or holding register on a Modbus TCP server (typically a PLC).
- **Output_Binding_Processor**: the device runtime's post-run handler
  (`OutputBindingProcessor` in `src/backend/workflow_engine/output_bindings.py`)
  that evaluates gating and dispatches each output executor binding through an
  injectable client seam, containing per-binding failures.
- **Modbus_Client**: the device-side Modbus TCP client function set the
  Output_Binding_Processor's default writer uses: frame encoding/decoding
  (MBAP header + PDU) for Write Single Coil (function code 0x05) and Write
  Single Register (function code 0x06), plus the socket exchange.
- **MBAP_Header**: the 7-byte Modbus Application Protocol header prefixed to
  every Modbus TCP frame: transaction id (2 bytes), protocol id (2 bytes,
  always 0), length (2 bytes), unit id (1 byte).
- **Modbus_Exception_Response**: a server reply whose function code has the
  high bit set (request function code + 0x80) carrying a one-byte exception
  code (e.g. 0x02 ILLEGAL DATA ADDRESS).
- **Value_Template**: the `value_template` string parameter rendered over the
  run's inference metadata by the shared `render_template` helper —
  placeholders `{is_anomalous}`, `{confidence}`, `{inference_json}`; a template
  that is exactly one known placeholder keeps the value's native type.
- **Conditional_Gating**: the existing compiler/executor mechanism by which
  output bindings downstream of a `conditional` node's "true"/"false" ports
  (compiler `portConditions`) or an `inference_filter` node run only when the
  gate condition evaluates true.
- **Recording_Stub**: the `ARCH_SIM` executor binding
  (`recording_<type_id>`) a hardware output node compiles to in simulation
  mode; the cloud test sandbox records the would-be actuation (parameters +
  triggering metadata) instead of contacting any endpoint.
- **Workflow_Validator**: the pure `validate(graph, catalog, ...)` function in
  `workflow_core/validator/checks.py`.
- **Workflow_Compiler**: the pure `compile(graph, target_arch, ...)` function
  in `workflow_core/compiler/compiler.py`.
- **Detail_Sink**: the optional `(node_id, detail)` callback the
  Output_Binding_Processor invokes with sent-message / skipped-outcome
  summaries (output-node-sent-message feature).

## Requirements

### Requirement 1: modbus_write catalog descriptor

**User Story:** As a workflow author, I want a Modbus TCP write output node in
the palette, so that a workflow run's result can actuate a PLC coil or register
the same way the existing digital/MQTT/OPC UA outputs actuate their endpoints.

#### Acceptance Criteria

1. THE Node_Type_Catalog SHALL define a `modbus_write` descriptor with
   `category=CATEGORY_OUTPUT`, display name "Modbus TCP Write", exactly one
   input port named `in` of type `PORT_TYPE_INFERENCE_META`, and zero output
   ports.
2. THE `modbus_write` descriptor SHALL declare connection parameters `host`
   (string, required, min_length 1), `port` (int, optional, default 502, min 1,
   max 65535), and `unit_id` (int, optional, default 1, min 0, max 255).
3. THE `modbus_write` descriptor SHALL declare a `register_type` enum parameter
   (required, default `coil`, values `coil` and `holding_register`) and an
   `address` int parameter (required, default None, min 0, max 65535).
4. THE `modbus_write` descriptor SHALL declare a `value_template` string
   parameter (optional, default `{is_anomalous}`) whose description documents
   the same placeholder set as `opcua_write`'s `value_template`
   (`{is_anomalous}`, `{confidence}`, `{inference_json}`; a single placeholder
   keeps its native type) plus the write coercion: coil writes coerce the
   rendered value to a boolean, holding-register writes to an integer 0-65535.
5. THE `modbus_write` descriptor SHALL declare a `pulse_ms` int parameter
   (optional, default 0, min 0, max 60000) visible only while `register_type`
   is `coil` (via the existing `depends_on` `"register_type=coil"` gating
   form), with the documented convention 0 = latch (single write) and a
   positive value = write the rendered value, wait `pulse_ms` milliseconds,
   then write the inverse coil value — mirroring `digital_output`'s pulse
   semantics.
6. THE `modbus_write` descriptor SHALL declare device-architecture mappings
   with `executor_binding="modbus_write"` and zero plugin dependencies, plus an
   `ARCH_SIM` Recording_Stub mapping (`recording_modbus_write`) in the
   `_recording_binding` form the three existing hardware outputs use, and SHALL
   set `hardware_dependent=True`.

### Requirement 2: Catalog integration, validation, and compilation discipline

**User Story:** As a platform maintainer, I want the new descriptor added
additively with zero validator or compiler changes, so that both catalog
copies, the baseline, and all existing compiled outputs stay byte-identical.

#### Acceptance Criteria

1. THE `modbus_write` descriptor SHALL be appended to `NODE_CATALOG` after all
   existing entries, with every pre-existing descriptor byte-identical to its
   pre-feature content.
2. THE portal catalog copy and the device-vendored catalog copy SHALL remain
   byte-identical after all changes in this feature.
3. WHEN the catalog baseline (`catalog_baseline.json`) is updated for this
   feature, THE updated baseline SHALL differ from the pre-feature baseline
   only in the `modbus_write` descriptor addition, with all other descriptor
   entries byte-identical.
4. WHEN `validate` is called on a graph whose `modbus_write` node lacks an
   effective value for `host`, `register_type`, or `address`, THE
   Workflow_Validator SHALL produce a `V4_MISSING_REQUIRED_PARAMETER` error
   finding for that node through the existing generic required-parameter
   check, with zero new check codes introduced by this feature.
5. WHEN a graph containing a `modbus_write` node is compiled for a device
   architecture, THE Workflow_Compiler SHALL emit one executor-binding entry
   with binding `modbus_write` carrying the node id, the node's effective
   parameters (declared defaults applied), and the node's upstream node ids —
   through the existing generic executor-binding emission with zero compiler
   changes.
6. WHEN a graph containing a `modbus_write` node is compiled with
   `simulation=True`, THE Workflow_Compiler SHALL resolve the node to its
   `recording_modbus_write` Recording_Stub binding, exactly as the three
   existing hardware outputs resolve today.
7. FOR ALL workflows containing no `modbus_write` node, THE compiled document
   emitted after this feature SHALL be byte-identical to the pre-feature
   compiled document for the same graph (verified against
   `golden_zero_trigger_compilation.json` and the packaging goldens, which
   SHALL remain unchanged).

### Requirement 3: Frontend catalog mirror and configuration panel

**User Story:** As a workflow author, I want the Modbus node in the designer
palette with a configuration panel that shows only the fields that apply, so
that I can configure a PLC write without seeing irrelevant options.

#### Acceptance Criteria

1. THE frontend catalog mirror (`types.ts`) SHALL define a `modbus_write`
   descriptor matching the backend descriptor in type id, category, display
   name, ports, and parameter names, types, defaults, constraints, and gating.
2. THE Node_Palette SHALL list the Modbus_Write_Node under the Outputs section
   exactly once.
3. WHILE a `modbus_write` node's effective `register_type` value is `coil`
   (including the unset default), THE Workflow_Designer configuration panel
   SHALL show the `pulse_ms` control, and WHILE the effective value is
   `holding_register`, THE configuration panel SHALL hide the `pulse_ms`
   control — through the existing `"name=value"` dependent-gating mechanism
   with zero gating-mechanism changes.
4. THE frontend inline checks (`inlineChecks.ts`) SHALL produce
   `V4_MISSING_REQUIRED_PARAMETER` findings for a `modbus_write` node missing
   `host`, `register_type`, or `address` through the existing generic V4
   mirror, with zero inline-check code changes, matching the
   Workflow_Validator findings for the same graph in code, severity, and node
   identifier.

### Requirement 4: Device Modbus TCP write execution

**User Story:** As a workflow operator, I want a deployed workflow's Modbus
output to write the configured coil or register on my PLC when the run result
reaches it, so that inspection outcomes drive real machine actions.

#### Acceptance Criteria

1. WHEN a run's output bindings are processed and a `modbus_write` binding is
   not gated out and its value renders successfully, THE
   Output_Binding_Processor SHALL write the rendered value to the configured
   `address` on the Modbus TCP server at `host`:`port` using the configured
   `unit_id`, over one TCP connection per binding execution that is closed
   after the exchange.
2. WHEN a `modbus_write` binding's `register_type` is `coil`, THE
   Output_Binding_Processor SHALL render the Value_Template over the run's
   inference metadata, coerce the rendered value to a boolean using the shared
   value normalization (`_coerce`), and issue a Write Single Coil (function
   code 0x05) request.
3. WHEN a `modbus_write` binding's `register_type` is `holding_register`, THE
   Output_Binding_Processor SHALL render the Value_Template, coerce the
   rendered value to an integer, and issue a Write Single Register (function
   code 0x06) request.
4. IF a holding-register binding's rendered value cannot be coerced to an
   integer in the range 0-65535, THEN THE Output_Binding_Processor SHALL fail
   that binding with an error naming the node, the rendered value, and the
   permitted range, and SHALL issue no write for that binding.
5. WHILE a coil binding's `pulse_ms` is greater than 0, WHEN the initial coil
   write succeeds, THE Output_Binding_Processor SHALL wait `pulse_ms`
   milliseconds and then write the inverse coil value to the same address;
   WHILE `pulse_ms` is 0, THE Output_Binding_Processor SHALL issue exactly one
   write (latch).
6. THE Output_Binding_Processor SHALL accept the Modbus writer by injection (a
   constructor argument with a production default), mirroring the existing
   `dio_actuator` / `mqtt_publisher` / `opcua_writer` seams, so tests run
   without a PLC or network access.
7. IF a `modbus_write` binding fails (connection error, timeout, exception
   response, or value coercion error), THEN THE Output_Binding_Processor SHALL
   log the failure, continue processing all other bindings unaffected, and
   aggregate the failure into the run's `OutputBindingError` naming the
   failing node id — the existing per-binding containment contract.
8. THE Modbus_Client SHALL bound the TCP connect and the response wait with a
   5-second timeout each, and WHEN a timeout elapses, THE
   Output_Binding_Processor SHALL fail that binding with an error naming the
   node, host, and port.
9. WHEN a `modbus_write` binding completes successfully, THE
   Output_Binding_Processor SHALL emit a Detail_Sink sent-message summary
   naming the written value, the register type and address, the host:port, and
   the unit id (plus the pulse duration when pulsed), in the existing
   sent-message detail form.

### Requirement 5: Modbus TCP frame encoding and decoding

**User Story:** As a platform maintainer, I want the Modbus TCP framing
implemented as small pure encode/decode functions with no new packaged
dependency, so that the protocol layer is property-testable and the
preservation-tracked dependency surface stays unchanged.

#### Acceptance Criteria

1. THE Modbus_Client SHALL encode Write Single Coil and Write Single Register
   requests as MBAP_Header-prefixed frames: a per-exchange transaction id,
   protocol id 0, the correct length field, the unit id, the function code
   (0x05 or 0x06), the 16-bit address, and the 16-bit value (0xFF00 for coil
   ON, 0x0000 for coil OFF; the register value verbatim), in big-endian byte
   order.
2. THE Modbus_Client SHALL decode a well-formed response frame into its
   transaction id, unit id, function code, address, and value fields, and
   WHEN a response echoes the request's function code, address, and value with
   a matching transaction id, THE Modbus_Client SHALL report the write as
   successful.
3. FOR ALL valid write requests (both function codes, all addresses 0-65535,
   all register values 0-65535, both coil states, all unit ids 0-255, and all
   transaction ids), decoding an encoded request frame SHALL yield the
   original field values (round-trip property).
4. IF the server returns a Modbus_Exception_Response, THEN THE Modbus_Client
   SHALL raise an error naming the exception code and its standard Modbus
   meaning (e.g. 0x01 ILLEGAL FUNCTION, 0x02 ILLEGAL DATA ADDRESS, 0x03
   ILLEGAL DATA VALUE, 0x04 SERVER DEVICE FAILURE).
5. IF a response frame is truncated, carries a mismatched transaction id, a
   non-zero protocol id, or an unexpected function code, THEN THE
   Modbus_Client SHALL raise an error describing the malformation rather than
   treating the write as successful.
6. THE Modbus_Client SHALL be implemented using only the Python standard
   library (socket/struct), with `src/backend/requirements.txt` and every
   other preservation-gate-tracked file unchanged by this feature.

### Requirement 6: Conditional gating and output-binding preservation

**User Story:** As a workflow author, I want the Modbus write to fire only on
the conditional branch I wired it behind, so that each "channel of action"
actuates exactly when its condition holds — and existing workflows keep
behaving exactly as before.

#### Acceptance Criteria

1. WHEN a `modbus_write` binding is directly downstream of a `conditional`
   output port whose gate condition evaluated false, or of an
   `inference_filter` whose condition did not pass, THE
   Output_Binding_Processor SHALL issue no Modbus write for that binding and
   SHALL emit the existing "not sent: gated out" Detail_Sink summary.
2. WHEN a `modbus_write` binding is directly downstream of a `conditional`
   output port whose gate condition evaluated true, THE
   Output_Binding_Processor SHALL execute the Modbus write for that binding.
3. WHEN a run's document wires multiple `modbus_write` bindings behind
   different `conditional` ports, THE Output_Binding_Processor SHALL evaluate
   each binding's gating independently, executing exactly the bindings whose
   gates passed.
4. FOR ALL compiled documents containing no `modbus_write` binding, THE
   Output_Binding_Processor's observable behavior (writes issued, details
   emitted, errors raised) SHALL be identical to the pre-feature processor for
   the same document and metadata.

### Requirement 7: Simulation recording stub

**User Story:** As a workflow author, I want test runs of a workflow containing
a Modbus output to record the intended writes instead of contacting a PLC, so
that I can validate my conditional wiring in the portal without hardware.

#### Acceptance Criteria

1. WHEN the cloud test sandbox executes a simulation document containing a
   `recording_modbus_write` binding whose gates passed, THE test sandbox SHALL
   record the binding's parameters and the triggering inference metadata as
   recorded stub activity for that node, contacting no endpoint — through the
   existing prefix-based recording path with zero sandbox code changes.
2. WHEN the cloud test sandbox executes a simulation document containing a
   `recording_modbus_write` binding that was gated out, THE test sandbox SHALL
   record the node as not triggered, mirroring the existing gated-recorder
   behavior for the three existing hardware outputs.
