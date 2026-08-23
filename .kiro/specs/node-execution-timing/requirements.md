# Requirements Document

## Introduction

This feature surfaces per-node execution timing for deployed-workflow runs on the edge device so operators can profile their workflows. Today the run status graph (deployed-workflow-run-observability) shows only each node's lifecycle state (pending/running/success/warning/failure), and the only timing available is the run-level startedAt/finishedAt on the WorkflowExecution. This feature adds two capabilities:

1. **Timing in the run status view** — after a node reaches success in the Run_Status_Graph, its execution time is displayed with the node.
2. **Timing in the run log** — each node's execution time is emitted as a log line into the per-run log (captured by the existing RunLogCapture), so timings are readable in the "View run log" view.

Because different node kinds execute differently, the feature defines timing semantics per node kind: GStreamer Pipeline_Nodes process frames continuously, so their execution time is the wall-clock interval from the node entering the `running` state to the node reaching a terminal state for the run; Executor_Binding_Nodes (LLM/Bedrock inference, MQTT publish, OPC UA write, digital output) run as discrete invocations, so their execution time is the wall-clock duration of the binding's invocation.

The work is scoped to the edge device only: the LocalServer backend (`src/backend`, in particular `workflow_engine`) and the on-device frontend (`src/frontend`). No cloud Portal changes are involved. It builds on the NodeStatusCollector, the node-status device API, the RunStatusGraph screen, and the RunLogCapture from prior specs.

## Glossary

- **LocalServer**: The Greengrass component running on an edge device that hosts the on-device backend (`src/backend`) and frontend (`src/frontend`).
- **WorkflowExecution**: The device-side record of one triggered run of a deployed workflow, with a run-level lifecycle status and startedAt/finishedAt timestamps.
- **WorkflowExecutor**: The `workflow_engine.pipeline_executor.WorkflowExecutor` that renders and runs the compiled pipeline and then runs output bindings, recording the execution result.
- **Node**: A logical workflow step identified by a `nodeId` in the workflow graph.
- **Pipeline_Node**: A Node that maps to one or more GStreamer pipeline elements in the compiled pipeline document; frames flow through it continuously while the pipeline plays.
- **Executor_Binding_Node**: A Node with no pipeline element that the WorkflowExecutor runs as a discrete invocation after or around the pipeline (for example `llm_inference`, `bedrock_inference`, `mqtt_publish`, `opcua_write`, `digital_output`).
- **Node_Run_Status**: The per-node execution state for a run: `pending`, `running`, `success`, `warning`, or `failure`, accumulated by the NodeStatusCollector.
- **NodeStatusCollector**: The `workflow_engine.node_status.NodeStatusCollector` that accumulates per-node status over a run and serializes the `{nodeId: {status, detail?}}` map persisted to the WorkflowExecution.
- **Node_Execution_Time**: The measured wall-clock duration attributed to one Node for one run. For a Pipeline_Node: the interval from the Node entering the `running` state to the Node reaching a terminal Node_Run_Status for the run. For an Executor_Binding_Node: the duration of that binding's invocation (from invocation start to invocation completion).
- **Run_Status_Graph**: The on-device screen (`RunStatusGraph.tsx`) that mirrors the workflow graph and colors each node by its Node_Run_Status, polling node status while a run is active.
- **Node_Status_Endpoint**: The device API endpoint that returns the per-node Node_Run_Status map for a WorkflowExecution, addressable by `nodeId`.
- **Run_Log**: The per-execution log captured by RunLogCapture into a bounded file under the run's output directory and viewable via "View run log" in the device UI.
- **RunLogCapture**: The context manager that attaches a bounded file handler to the `workflow_engine` and `gstreamer` loggers for the duration of a run's `execute()`.

## Requirements

### Requirement 1: Measure per-node execution time

**User Story:** As an operator profiling a deployed workflow, I want the device to measure how long each node took during a run, so that I can identify slow steps.

#### Acceptance Criteria

1. WHEN a Node reaches a terminal Node_Run_Status (success, warning, or failure) for the first time during a run, THE NodeStatusCollector SHALL record that Node's Node_Execution_Time exactly once for that run, and SHALL NOT overwrite the recorded value on any subsequent status transition for the same Node in the same run (including status re-touches during finalization).
2. THE NodeStatusCollector SHALL measure a Pipeline_Node's Node_Execution_Time as the interval from the first moment the Node enters the `running` state to the first moment the Node reaches a terminal Node_Run_Status for the run.
3. THE WorkflowExecutor SHALL measure an Executor_Binding_Node's Node_Execution_Time as the duration of that binding's invocation, from invocation start to invocation completion, where invocation completion includes both normal return and termination by an error.
4. WHEN an Executor_Binding_Node's binding invocation completes, THE WorkflowExecutor SHALL record that Node's Node_Execution_Time regardless of the Node's eventual Node_Run_Status, and this invocation duration SHALL be the retained Node_Execution_Time value for that Node, taking precedence over any value recorded under criterion 1.
5. THE NodeStatusCollector SHALL measure Node_Execution_Time using a monotonic clock and SHALL record the value as a non-negative duration with at least millisecond resolution.
6. IF a Node reaches a terminal Node_Run_Status without ever having entered the `running` state, THEN THE NodeStatusCollector SHALL record no Node_Execution_Time for that Node.
7. IF measuring or recording a Node_Execution_Time raises an error, THEN THE NodeStatusCollector SHALL catch the error without re-raising it, SHALL record no partial Node_Execution_Time value for the affected Node, and the run SHALL produce Node_Run_Status outcomes for every Node identical to those of a run in which no measurement error occurred.
8. WHEN per-node status output is produced for a run, THE NodeStatusCollector SHALL expose each Node's recorded Node_Execution_Time alongside that Node's Node_Run_Status in the per-node status output.

### Requirement 2: Persist and expose per-node execution times

**User Story:** As the on-device frontend, I want each node's execution time available through the existing node-status API, so that the run status view can display it without new endpoints.

#### Acceptance Criteria

1. WHEN a run's node-status map is persisted to the WorkflowExecution, THE NodeStatusCollector SHALL include each recorded Node_Execution_Time in that Node's entry as an additive field expressed in milliseconds as a non-negative integer, rounded to the nearest millisecond.
2. WHEN the node status of a WorkflowExecution is requested, THE Node_Status_Endpoint SHALL return every Node_Execution_Time available for that WorkflowExecution alongside that Node's Node_Run_Status.
3. IF a Node has no recorded Node_Execution_Time, THEN THE Node_Status_Endpoint SHALL return that Node's entry with its Node_Run_Status and without the timing field.
4. THE node-status map serialization SHALL preserve the existing fields (`status`, `detail`) with their current names and meanings, and SHALL add the timing field without removing, renaming, or changing the type of any existing field.
5. WHILE a WorkflowExecution is still active, WHEN the node status is requested, THE Node_Status_Endpoint SHALL return the Node_Execution_Time of each Node that has already reached a terminal Node_Run_Status in that run.
6. IF a WorkflowExecution was persisted before timing data was introduced and its node-status map contains no timing fields, THEN THE Node_Status_Endpoint SHALL return that WorkflowExecution's node-status map without error and without timing fields.

### Requirement 3: Display execution time in the run status graph

**User Story:** As an operator watching a run in the run status view, I want to see each node's execution time once the node succeeds, so that I can profile the workflow at a glance.

#### Acceptance Criteria

1. WHILE a Node's Node_Run_Status is `success` and a Node_Execution_Time is recorded for that Node, THE Run_Status_Graph SHALL display that Node's Node_Execution_Time with the Node.
2. WHILE a Node's Node_Run_Status is `warning` or `failure` and a Node_Execution_Time is recorded for that Node, THE Run_Status_Graph SHALL display that Node's Node_Execution_Time with the Node.
3. THE Run_Status_Graph SHALL format each displayed Node_Execution_Time as follows: durations under 1000 milliseconds as the whole-millisecond value followed by " ms" (for example "412 ms"), and durations of 1000 milliseconds or more as seconds with exactly one decimal place, rounded to the nearest tenth of a second, followed by " s" (for example "3.4 s").
4. WHEN a recorded Node_Execution_Time is 0 milliseconds, THE Run_Status_Graph SHALL display "0 ms" for that Node.
5. WHILE a Node's Node_Run_Status is `pending` or `running`, THE Run_Status_Graph SHALL display that Node without an execution-time element.
6. IF a Node's Node_Run_Status is `success`, `warning`, or `failure` and no Node_Execution_Time is recorded for that Node, THEN THE Run_Status_Graph SHALL display that Node with its existing status coloring and detail rendering and without an execution-time element.
7. WHEN a node-status polling response for an active run includes a Node_Execution_Time that is not currently displayed, THE Run_Status_Graph SHALL display that Node_Execution_Time in the rendering of that polling response, without a page reload.
8. IF a recorded Node_Execution_Time is negative or non-numeric, THEN THE Run_Status_Graph SHALL display that Node without an execution-time element.

### Requirement 4: Emit per-node execution times to the run log

**User Story:** As an operator reading a run's log, I want each node's execution time recorded as log lines, so that I can profile a run from the "View run log" view and from retained logs.

#### Acceptance Criteria

1. WHEN a Node's Node_Execution_Time is recorded during a run, THE WorkflowExecutor SHALL emit exactly one log line for that Node, stating the Node's `nodeId` and its Node_Execution_Time, to the `workflow_engine` logger while the run's RunLogCapture is active (that is, before the run's `execute()` completes).
2. THE per-node timing log line SHALL state the duration using the same formatting rules as the Run_Status_Graph (Requirement 3.3): durations under 1 second in whole milliseconds (for example "412 ms"), durations of 1 second or more in seconds with one decimal place (for example "3.4 s"), and a recorded duration of 0 milliseconds as "0 ms".
3. WHEN a run's Run_Log capture ends, THE Run_Log SHALL contain exactly one timing log line for each Node that has a recorded Node_Execution_Time and no timing log line for any Node without a recorded Node_Execution_Time, regardless of when the run reached its terminal state, except where the Run_Log's existing bounded-capacity behavior has evicted older lines.
4. IF emitting a timing log line for a Node raises an error, THEN THE WorkflowExecutor SHALL contain the error, SHALL still attempt to emit timing log lines for the remaining Nodes, and the run SHALL proceed to the same terminal state it would have reached without the error.

### Requirement 5: Non-regression and isolation

**User Story:** As a maintainer, I want the timing additions to leave existing run behavior intact, so that current workflows, endpoints, and tests are unaffected.

#### Acceptance Criteria

1. THE Node_Run_Status lifecycle SHALL produce, for any given sequence of run events, the same per-node state transitions (pending → running → success/warning/failure, via mark_running_all, mark_success_all, mark_failure, and finalize) as it produces without the timing feature, with no new states, no new transitions, and no changed transition ordering introduced.
2. THE Device API SHALL retain every existing response field with its current name, type, and meaning in every existing endpoint; timing data SHALL appear only as additive new fields, and no existing field SHALL be removed, renamed, or repurposed.
3. WHEN a run executes with the timing feature present, THE run SHALL reach the same terminal state (success, warning, or failure) and the same final per-node Node_Run_Status map that it would reach for the same inputs without the timing feature.
4. IF timing measurement, timing persistence, or timing logging raises an error during a run, THEN THE Workflow Engine SHALL contain the error without propagating it to the run, SHALL leave the run's terminal state, Node_Run_Status transitions, and Device API responses unaltered, and SHALL record the error only as a log entry indicating the timing failure.
5. THE existing backend test suite and THE existing frontend test suite SHALL pass with zero failures and with no modification to any pre-existing test assertion, and each code change introduced by the timing feature SHALL be covered by at least one new test in the same layer as the changed code (backend tests for src/backend changes, frontend tests for src/frontend changes).
