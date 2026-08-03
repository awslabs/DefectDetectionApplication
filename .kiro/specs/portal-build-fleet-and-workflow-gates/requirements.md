# Requirements Document

## Introduction

This feature adds two related capability areas to the edge CV portal:

1. **Portal-driven edge component builds with fleet management.** Today, JP5/JP6 Greengrass component builds require a manually launched ARM64 EC2 build server (`launch-arm64-build-server.sh`), SSH access, and command-line build/publish scripts. This feature lets users trigger edge target builds (JP5, JP6, AMD64, AMD64_NVIDIA) from the portal UX, monitor build progress, and publish artifacts without touching a terminal. It supports two execution modes: **ephemeral compute** (build compute is provisioned on demand, runs one build, publishes, and terminates so nothing costs money while idle) and **dedicated build servers** (persistent EC2 instances the user can start, stop, launch, and terminate from a portal fleet management page). The specific ephemeral compute service (ECS, EKS, EC2 spot, or similar) is an investigation area deferred to design; the requirements below express the required behavior independent of the service choice. JP5 and JP6 builds are ARM64; AMD64 and AMD64_NVIDIA builds are x86_64, and the build compute's CPU architecture must match the Build_Target's architecture. All builds are long-running (approximately 1 to 2 hours each), and per the repository build rules, two builds must never run concurrently on the same build server because concurrent builds corrupt model versioning.

2. **Chat workflow generation gates.** The portal's chat-based workflow generation (Bedrock-backed `workflow_generator.py`) sometimes produces node graphs that cannot work: incompatible port connections, backwards (reversed) edges, cycles, dangling or unreachable nodes, missing input or output nodes, or node types that cannot coexist. This feature adds generation-time gates that validate every generated workflow before it is presented or persisted, attempts automatic correction when possible, and gives the user clear feedback when a generation is rejected.

## Glossary

- **Portal**: The edge CV portal web application (React/Cloudscape frontend, Lambda backend, CDK infrastructure under `edge-cv-portal/`).
- **Build_Target**: One of the four supported edge component targets: JP5 (ARM64, `aws.edgeml.dda.LocalServer.arm64JP5`), JP6 (ARM64, `aws.edgeml.dda.LocalServer.arm64JP6`), AMD64 (x86_64, `aws.edgeml.dda.LocalServer.amd64`), or AMD64_NVIDIA (x86_64, `aws.edgeml.dda.LocalServer.amd64Nvidia`). Each Build_Target requires build compute of a matching CPU architecture: ARM64 for JP5 and JP6, x86_64 for AMD64 and AMD64_NVIDIA.
- **Build_Job**: A single request to build and publish one Build_Target, tracked from submission through completion with a status, logs, and result metadata.
- **Build_Manager**: The portal backend capability that accepts Build_Job requests, dispatches them to build compute, tracks their status, and enforces build serialization.
- **Build_Server**: Any compute instance (ephemeral or dedicated) on which a Build_Job executes.
- **Dedicated_Build_Server**: A persistent EC2 instance provisioned for builds, whose CPU architecture (ARM64 or x86_64) is chosen at launch, managed through the Fleet_Manager (replaces the manual `launch-arm64-build-server.sh` flow).
- **Ephemeral_Build_Runner**: Build compute provisioned on demand for a single Build_Job with the CPU architecture required by that Build_Job's Build_Target, and terminated after the Build_Job finishes, so no build compute runs while no build is active.
- **Fleet_Manager**: The portal capability (backend and UX) for listing, launching, starting, stopping, and terminating Dedicated_Build_Servers.
- **Build_Queue**: The ordered set of Build_Jobs waiting for a Build_Server to become available.
- **Artifact_Publisher**: The build-completion step that publishes the built component (Greengrass component version and container images) to its target registries.
- **Portal_Admin**: A user holding the portal's administrative role (existing PortalAdmin role).
- **Build_Operator**: A user holding the permission to submit Build_Jobs and view build status.
- **Audit_Log**: The portal's existing audit logging facility.
- **Workflow_Definition**: The JSON graph document describing a workflow (nodes, parameters, connections), as produced by the Workflow_Generator and consumed by the workflow canvas.
- **Workflow_Generator**: The existing Bedrock-backed chat generation Lambda (`workflow_generator.py`) that produces Workflow_Definitions from natural-language prompts.
- **Workflow_Validator**: The existing validation engine (`workflow_core.validator`, surfaced through `workflow_validation.py`) that produces findings with error or warning severity for a Workflow_Definition.
- **Generation_Gate**: The new gate applied to every generated Workflow_Definition that classifies structural validity and decides whether the definition is presented, auto-corrected, or rejected.
- **Structural_Error**: An error-severity validation finding that makes a node graph unable to function: an incompatible port connection, a backwards edge (a connection whose endpoints are not an output port joined to an input port, or whose direction contradicts processing order), a cycle, a dangling or unreachable node, a missing input-category or output-category node, or a combination of node types that cannot coexist in one workflow.
- **Repair_Pass**: A single automatic re-invocation of the Workflow_Generator that includes the Structural_Errors of the previous attempt and instructs the model to correct them.
- **Unrepairable_Error**: A Structural_Error the Generation_Gate classifies as not correctable by a Repair_Pass; the classification rules are defined during design.

## Requirements

### Requirement 1: Edge Target Build Initiation from the Portal

**User Story:** As a Build_Operator, I want to trigger JP5, JP6, AMD64, and AMD64_NVIDIA component builds from the portal, so that I can produce and publish edge components for every supported device architecture without SSH or the command line.

#### Acceptance Criteria

1. THE Portal SHALL provide a build page where a Build_Operator can select one or more Build_Targets (JP5, JP6, AMD64, AMD64_NVIDIA) and submit a build request.
2. WHEN a Build_Operator submits a build request for one Build_Target, THE Build_Manager SHALL create one Build_Job for that Build_Target.
3. WHEN a Build_Operator submits a build request selecting two or more Build_Targets, THE Build_Manager SHALL create one Build_Job per selected Build_Target ordered by the sequence in which the Build_Targets appear in the build request, and SHALL start each Build_Job after the first only after the preceding Build_Job in that order reaches one of the terminal statuses (succeeded, failed, interrupted, cancelled), regardless of which terminal status the preceding Build_Job reached.
4. IF a build request selects a target that is not one of JP5, JP6, AMD64, or AMD64_NVIDIA, THEN THE Build_Manager SHALL reject the request without creating any Build_Job and return a validation error identifying the supported Build_Targets (JP5, JP6, AMD64, AMD64_NVIDIA).
5. WHEN a Build_Job is created, THE Build_Manager SHALL record the requesting user, the Build_Target, the selected execution mode (ephemeral or dedicated), and the submission time.
6. IF a user without the Build_Operator permission submits a build request, THEN THE Build_Manager SHALL reject the request without creating a Build_Job, return an authorization error, and record a denied-access entry in the Audit_Log containing the requesting user, the attempted action (build request submission), and the time of the attempt.
7. WHEN a Build_Job is created, THE Build_Manager SHALL record a build-requested entry in the Audit_Log containing the requesting user, the Build_Target, the selected execution mode (ephemeral or dedicated), and the submission time.
8. IF a build request selects zero Build_Targets, THEN THE Build_Manager SHALL reject the request without creating any Build_Job and return a validation error indicating that at least one Build_Target must be selected.
9. WHEN a Build_Job is created, THE Build_Manager SHALL assign the Build_Job the queued status as its initial status.

### Requirement 2: Build Execution Mode Selection

**User Story:** As a Build_Operator, I want to choose between ephemeral build compute and my dedicated build servers, so that I can trade off cost, availability, and control per build.

#### Acceptance Criteria

1. THE Portal SHALL present on the build page, before a build request can be submitted, a selection of exactly one execution mode: Ephemeral_Build_Runner or a specific Dedicated_Build_Server identified from the fleet list.
2. WHEN a Build_Job whose build request selected the dedicated execution mode is dispatched, THE Build_Manager SHALL dispatch the Build_Job only to the Dedicated_Build_Server selected in the build request.
3. WHEN a Build_Job whose build request selected the ephemeral execution mode is dispatched, THE Build_Manager SHALL provision exactly one Ephemeral_Build_Runner for that Build_Job.
4. IF the dedicated execution mode is selected and the selected Dedicated_Build_Server is in any lifecycle state other than running (such as pending, stopping, stopped, or terminated) or does not exist in the fleet, THEN THE Build_Manager SHALL reject the build request without creating a Build_Job and return an error identifying the server's current lifecycle state (or that the server does not exist) and the action needed to proceed.
5. WHILE the fleet contains no Dedicated_Build_Server in a lifecycle state other than terminated, THE Portal SHALL present the ephemeral execution mode as the only selectable execution mode.
6. IF a build request omits the execution mode selection, or selects the dedicated execution mode without identifying a specific Dedicated_Build_Server, THEN THE Build_Manager SHALL reject the request without creating a Build_Job and return a validation error identifying the missing selection.
7. WHEN a build request selecting two or more Build_Targets is submitted, THE Build_Manager SHALL apply the execution mode selected in the build request, including the selected Dedicated_Build_Server for the dedicated execution mode, to every Build_Job created for that request.
8. IF the dedicated execution mode is selected and the selected Dedicated_Build_Server's CPU architecture does not match the CPU architecture required by a selected Build_Target (ARM64 for JP5 and JP6, x86_64 for AMD64 and AMD64_NVIDIA), THEN THE Build_Manager SHALL reject the build request without creating any Build_Job and return an error identifying the selected server's CPU architecture, the mismatched Build_Target, and the CPU architecture that Build_Target requires.

### Requirement 3: Ephemeral Build Lifecycle and Zero Idle Cost

**User Story:** As a Portal_Admin, I want build compute to exist only while a build is running, so that builds incur no idle infrastructure cost.

#### Acceptance Criteria

1. WHEN a Build_Job is dispatched in ephemeral mode, THE Build_Manager SHALL set the Build_Job status to provisioning and begin provisioning an Ephemeral_Build_Runner with the CPU architecture required by the Build_Job's Build_Target, using the ephemeral compute sizing parameters from portal configuration, within 60 seconds of dispatch.
2. WHEN an Ephemeral_Build_Runner finishes its Build_Job (any terminal status), THE Build_Manager SHALL terminate the Ephemeral_Build_Runner within 10 minutes of the Build_Job reaching the terminal status.
3. WHILE no Build_Job in ephemeral mode is queued or running, THE Build_Manager SHALL keep zero Ephemeral_Build_Runners provisioned.
4. WHEN an Ephemeral_Build_Runner is terminated, THE Build_Manager SHALL retain the Build_Job's complete build log output and result metadata (terminal status, start time, end time, and published artifact identifiers when present) in durable storage independent of the runner for a minimum of 90 days.
5. IF the compute provider reclaims or interrupts an Ephemeral_Build_Runner before its Build_Job completes, THEN THE Build_Manager SHALL mark the Build_Job with an interrupted status, retain the logs produced up to the interruption, and present a retry action to the Build_Operator.
6. WHEN a Build_Operator invokes the retry action on an interrupted Build_Job, THE Build_Manager SHALL create a new Build_Job with the same Build_Target and execution mode as the interrupted Build_Job and record a reference from the new Build_Job to the interrupted Build_Job.
7. IF provisioning an Ephemeral_Build_Runner fails, THEN THE Build_Manager SHALL mark the Build_Job as failed with an error indicating the provisioning failure cause, terminate any partially provisioned build compute within 10 minutes of the failure, and record the failure in the Audit_Log.
8. WHEN a Build_Job in ephemeral mode exceeds a configurable maximum runtime (default 4 hours), THE Build_Manager SHALL terminate the Ephemeral_Build_Runner, mark the Build_Job as failed with a timeout error, and retain the logs produced up to termination.
9. IF termination of an Ephemeral_Build_Runner fails, THEN THE Build_Manager SHALL retry the termination at intervals of no more than 10 minutes for up to 1 hour and, when the runner is still not terminated after the retry period, notify Portal_Admins of the orphaned runner and record the termination failure in the Audit_Log.

### Requirement 4: Build Job Status and Monitoring

**User Story:** As a Build_Operator, I want to watch build progress and read logs from the portal, so that I know whether my 1-to-2-hour build is healthy without SSH access.

#### Acceptance Criteria

1. THE Build_Manager SHALL track each Build_Job through exactly one of the following statuses at any time: queued, provisioning, building, publishing, succeeded, failed, interrupted, cancelled, of which succeeded, failed, interrupted, and cancelled are terminal statuses that a Build_Job never leaves once reached.
2. WHEN a Build_Job status changes, THE Portal SHALL display the new status within 30 seconds of the change.
3. THE Portal SHALL display, for each Build_Job: the Build_Target, execution mode, requesting user, and submission time from the moment the Build_Job is created; the assigned Build_Server from the moment a Build_Server is assigned to the Build_Job; the start time from the moment the Build_Job enters the building status; and the end time from the moment the Build_Job reaches a terminal status.
4. THE Portal SHALL display new build log output of a running Build_Job within 60 seconds of the output being produced, and SHALL provide access to the complete build log of each Build_Job for at least 90 days after the Build_Job reaches a terminal status.
5. WHEN a Build_Operator requests cancellation of a queued Build_Job, THE Build_Manager SHALL remove the Build_Job from the Build_Queue, mark the Build_Job cancelled, and record the cancellation in the Audit_Log.
6. WHEN a Build_Operator requests cancellation of a running Build_Job, THE Build_Manager SHALL stop the build process on its Build_Server within 5 minutes of the cancellation request, mark the Build_Job cancelled, and record the cancellation in the Audit_Log.
7. THE Portal SHALL display a history of all Build_Jobs from the preceding 90 days, ordered most recent first, showing each Build_Job's terminal status and, for each Build_Job with the succeeded status, its published artifact identifiers.
8. IF a Build_Operator requests cancellation of a Build_Job that is already in a terminal status, THEN THE Build_Manager SHALL reject the request without changing the Build_Job and return an error identifying the Build_Job's current status.
9. IF the build process of a running Build_Job is not confirmed stopped within 5 minutes of a cancellation request, THEN THE Build_Manager SHALL keep the Build_Job in its current status rather than marking it cancelled, return an error to the requesting Build_Operator identifying the affected Build_Server, and record the failed cancellation in the Audit_Log.
10. IF a user without the Build_Operator permission requests cancellation of a Build_Job, THEN THE Build_Manager SHALL reject the request without changing the Build_Job, return an authorization error, and record a denied-access entry in the Audit_Log.

### Requirement 5: Artifact Publishing

**User Story:** As a Build_Operator, I want successful builds to publish their artifacts automatically, so that a completed build is immediately usable for deployment.

#### Acceptance Criteria

1. WHEN a Build_Job's build step completes successfully, THE Build_Manager SHALL set the Build_Job status to publishing and invoke the Artifact_Publisher for the built component.
2. WHEN the Artifact_Publisher is invoked for a Build_Job, THE Artifact_Publisher SHALL publish the built component as a Greengrass component version and push each associated container image to its target registry.
3. WHEN the Artifact_Publisher completes all publishing actions successfully (the Greengrass component version is published and every associated container image is pushed), THE Build_Manager SHALL record the published component version identifier and the pushed image references on the Build_Job and set the Build_Job status to succeeded.
4. IF any publishing action fails after a successful build step, THEN THE Build_Manager SHALL mark the Build_Job as failed with a publishing error that distinguishes the publish failure from a build failure, record on the Build_Job which artifacts were published before the failure and which were not, retain the build logs and the publishing logs, and record the publishing failure in the Audit_Log.
5. WHEN a Build_Job reaches the succeeded status, THE Build_Manager SHALL record a build-published entry in the Audit_Log including the published component version identifier and image references.

### Requirement 6: Dedicated Build Server Fleet Management

**User Story:** As a Portal_Admin, I want to launch, start, stop, and terminate dedicated build servers from the portal, so that I can manage build capacity without the command line or the manual launch script.

#### Acceptance Criteria

1. THE Fleet_Manager SHALL display a list of all Dedicated_Build_Servers with, for each server: name, instance identifier, instance type, CPU architecture (ARM64 or x86_64), lifecycle state (exactly one of: pending, running, stopping, stopped, shutting-down, terminated), the Build_Job currently running on the server when one exists, and the time of the last state change.
2. WHEN a Portal_Admin requests to start a Dedicated_Build_Server that is in the stopped state, THE Fleet_Manager SHALL initiate the start and display each lifecycle state transition until the server reaches the running state.
3. WHEN a Portal_Admin requests to stop a Dedicated_Build_Server that is in the running state and has no running Build_Job, THE Fleet_Manager SHALL initiate the stop and display each lifecycle state transition until the server reaches the stopped state.
4. IF a Portal_Admin requests to stop or terminate a Dedicated_Build_Server while a Build_Job is running on that server, THEN THE Fleet_Manager SHALL reject the request with an error identifying the running Build_Job and instructing the user to cancel the Build_Job or wait for it to finish.
5. WHEN a Portal_Admin requests to launch a new Dedicated_Build_Server with a server name and a CPU architecture selection (ARM64 or x86_64), THE Fleet_Manager SHALL provision an EC2 instance of the selected CPU architecture using the instance type configured for that architecture and the configured volume size, install the build environment on it, and register the server in the fleet list under the provided name with its CPU architecture.
6. WHEN a Portal_Admin requests to terminate a Dedicated_Build_Server that has no running Build_Job, THE Fleet_Manager SHALL require an explicit confirmation, then terminate the instance and mark the server terminated in the fleet list.
7. IF a user without the Portal_Admin role requests a fleet management action (launch, start, stop, terminate), THEN THE Fleet_Manager SHALL reject the request without performing the action, return an authorization error, and record a denied-access entry in the Audit_Log.
8. WHEN a fleet management action (launch, start, stop, terminate) completes or fails, THE Fleet_Manager SHALL record the action, the acting user, the target server, and the outcome in the Audit_Log.
9. THE Fleet_Manager SHALL refresh the displayed lifecycle state of each Dedicated_Build_Server within 30 seconds of a state change.
10. IF a Portal_Admin requests a fleet management action on a Dedicated_Build_Server whose current lifecycle state does not permit that action (for example, start requested while the server is not stopped, or stop requested while the server is not running), THEN THE Fleet_Manager SHALL reject the request without changing the server and return an error identifying the server's current lifecycle state.
11. IF an accepted fleet management action fails, or the target Dedicated_Build_Server does not reach the expected lifecycle state within 10 minutes of the action being initiated, THEN THE Fleet_Manager SHALL display an error identifying the action, the target server, and the server's current lifecycle state, and record the failure in the Audit_Log.
12. IF a Portal_Admin cancels or does not complete the termination confirmation, THEN THE Fleet_Manager SHALL perform no termination and leave the Dedicated_Build_Server unchanged.

### Requirement 7: Build Serialization Guarantee

**User Story:** As a Build_Operator, I want the system to guarantee that no two builds ever run at the same time on one build server, so that concurrent builds cannot corrupt model versioning.

#### Acceptance Criteria

1. THE Build_Manager SHALL run at most one Build_Job at a time on any single Build_Server.
2. WHEN a Build_Job is dispatched to a Dedicated_Build_Server that is already running a Build_Job, THE Build_Manager SHALL place the new Build_Job in the Build_Queue for that server instead of starting it.
3. WHEN a Build_Server's current Build_Job reaches a terminal status (succeeded, failed, interrupted, or cancelled), THE Build_Manager SHALL start the oldest queued Build_Job for that server, in submission order, within 5 minutes of the terminal status being recorded.
4. THE Build_Manager SHALL provision each Ephemeral_Build_Runner for exactly one Build_Job.
5. WHEN dispatching a Build_Job to a Dedicated_Build_Server, THE Build_Manager SHALL verify on the server that no build process is currently running before starting the build.
6. IF the pre-dispatch verification finds a build process already running on the Dedicated_Build_Server, THEN THE Build_Manager SHALL defer the Build_Job by returning it to the head of that server's Build_Queue with the queued status, and SHALL re-attempt the dispatch verification at intervals of 5 minutes until the verification finds no running build process or the Build_Job is cancelled.
7. WHILE a Build_Job is running on a Dedicated_Build_Server, THE Build_Manager SHALL check the server for concurrently running build processes at intervals not exceeding 5 minutes.
8. IF the Build_Manager detects two or more build processes running concurrently on one Build_Server, THEN THE Build_Manager SHALL stop every detected build process within 60 seconds of detection, mark each associated Build_Job as failed with a serialization-violation error, retain the logs produced by each Build_Job up to the stop, and record the event in the Audit_Log.
9. IF a Dedicated_Build_Server enters a stopped or terminated state while Build_Jobs remain in its Build_Queue, THEN THE Build_Manager SHALL mark each queued Build_Job for that server as failed with an error identifying the server state, and record the event in the Audit_Log.

### Requirement 8: Chat Workflow Generation Gates

**User Story:** As a workflow author using chat generation, I want the system to block invalid generated workflows, so that the canvas never receives a node graph that cannot work.

#### Acceptance Criteria

1. WHEN the Workflow_Generator produces a Workflow_Definition, THE Generation_Gate SHALL run the Workflow_Validator on the definition before the definition is returned to the client or persisted as the session canvas snapshot.
2. THE Generation_Gate SHALL classify as Structural_Errors at minimum: connections joining incompatible port types, backwards edges (endpoints that are not an output port joined to an input port), cycles in the node graph, nodes unreachable from any input-category node, connections referencing nonexistent nodes or ports, absence of an input-category node, absence of an output-category node, and combinations of node types that the Workflow_Validator reports as unable to coexist.
3. WHEN the Generation_Gate finds no Structural_Errors in a generated Workflow_Definition, THE Workflow_Generator SHALL return the definition to the client together with the complete findings list.
4. WHEN the Generation_Gate finds one or more Structural_Errors in a generated Workflow_Definition and classifies none of them as Unrepairable_Errors, THE Generation_Gate SHALL execute exactly one Repair_Pass before deciding rejection, and SHALL NOT execute more than one Repair_Pass per generation request.
5. IF the Generation_Gate classifies any Structural_Error in a generated Workflow_Definition as an Unrepairable_Error, THEN THE Workflow_Generator SHALL reject the generation without executing a Repair_Pass, with a response that lists each Structural_Error in user-readable form, leaves the session canvas snapshot unchanged, and preserves the user's prompt for retry.
6. WHEN a Repair_Pass produces a Workflow_Definition with no Structural_Errors, THE Workflow_Generator SHALL return the repaired definition to the client together with the complete findings list, the original Structural_Errors that were corrected, and an indication that an automatic correction was applied.
7. IF the Repair_Pass fails to complete or its result still contains Structural_Errors, THEN THE Workflow_Generator SHALL reject the generation with a response that lists each Structural_Error in user-readable form (the original Structural_Errors when the Repair_Pass did not complete), leaves the session canvas snapshot unchanged, and preserves the user's prompt for retry.
8. WHEN a generation is rejected by the Generation_Gate, THE Portal SHALL display the Structural_Errors to the user with, for each error, the affected nodes or connections identified by their identifier or display name, and a plain-language explanation of why the graph cannot work.
9. WHEN a generation is rejected by the Generation_Gate, THE Workflow_Generator SHALL leave the chat session message history and canvas snapshot in the state they had before the rejected generation.
10. IF the Workflow_Generator output cannot be parsed as a Workflow_Definition, THEN THE Workflow_Generator SHALL reject the generation with a response containing a user-readable error indicating the generation failed, leave the session canvas snapshot and chat session message history unchanged, and preserve the user's prompt for retry.
11. IF the Workflow_Validator fails to complete validation of a generated Workflow_Definition, THEN THE Generation_Gate SHALL reject the generation with a response containing a user-readable error indicating validation could not be completed, leave the session canvas snapshot and chat session message history unchanged, and preserve the user's prompt for retry.

### Requirement 9: Build Infrastructure Configuration

**User Story:** As a Portal_Admin, I want the build infrastructure parameters to be configurable, so that the system can adapt to account, region, and cost constraints without code changes.

#### Acceptance Criteria

1. WHEN a Build_Job is dispatched or a Dedicated_Build_Server launch is initiated, THE Build_Manager SHALL read the following from portal configuration: the Ephemeral_Build_Runner sizing parameters (CPU, memory, and storage), the maximum Build_Job runtime, the Dedicated_Build_Server instance type per CPU architecture (one instance type for ARM64 and one for x86_64) and volume size used for launches, and the AWS region for build compute.
2. IF a configuration value is absent when read, THEN THE Build_Manager SHALL apply the documented default for that parameter matching the current manual process: ARM64 instance type m6g.4xlarge, x86_64 instance type m6i.4xlarge, volume size 100 GB, AWS region us-east-1, and maximum Build_Job runtime 4 hours.
3. WHEN a build infrastructure configuration value changes, THE Build_Manager SHALL apply the new value only to Build_Jobs created after the change and Dedicated_Build_Server launches initiated after the change, and SHALL continue applying to each Build_Job that is queued or running at the time of the change the configuration values in effect at that Build_Job's creation.
4. WHEN a Portal_Admin changes a build infrastructure configuration value, THE Build_Manager SHALL record in the Audit_Log the changed parameter, the prior value, the new value, the acting user, and the time of the change.
5. IF a submitted configuration value is invalid (an instance type whose CPU architecture does not match the CPU architecture the instance type is configured for, a volume size that is not a positive number, or a maximum Build_Job runtime that is not a positive duration), THEN THE Build_Manager SHALL reject the change with a validation error identifying the invalid parameter and retain the prior configuration value.
6. IF a user without the Portal_Admin role requests a build infrastructure configuration change, THEN THE Build_Manager SHALL reject the request without applying the change, return an authorization error, and record a denied-access entry in the Audit_Log.
