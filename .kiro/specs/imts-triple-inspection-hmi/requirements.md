# Requirements Document

## Introduction

The IMTS Triple Inspection HMI is a special-purpose variant of the existing Quality Station HMI (`hmi/`, built from the `quality-station-hmi` spec). It runs full screen on a 1920x1080 monitor and displays, in real time, the results of the "blue-plate-detection-guided-inspection" workflow — a workflow that inspects **three parts simultaneously per run cycle**. The inspected parts are rectangular plates approximately 1.5 inches by 3 inches (a roughly 1:2 aspect ratio).

Where the existing HMI shows one inspection per run (one reference/captured image pair plus a verdict), the Triple Inspection HMI shows **all three inspections of a run at once**: for each of the three inspections, the annotated inspected image with defect bounding boxes overlaid is displayed side by side with the original un-annotated image.

The HMI reuses the existing architecture wholesale: it is a static-asset browser application served from the LocalServer's `/hmi` mount pattern, authenticates via `POST /local-auth/login`, discovers workflows via `GET /workflows/registrations`, detects new runs by polling the bounded recent-executions route (`GET /workflows/registrations/{id}/executions?limit=N`), reads each run's image inventory from `GET /workflows/executions/{id}/results` and verdict fields from `.../metadata`, and loads image bytes through the token-in-query image routes (`/output-image`, `/node-image?nodeId=&port=`).

Grounding in the existing artifact model: the backend persists per-node frames as `{capture_id}.node.{nodeId}.{port}.jpg` and lists them in `/results` deterministically sorted by nodeId then port (`run_artifacts.list_node_images`). Today the only annotated artifact is the single run-level overlay (`{capture_id}.overlay.jpg`); node entries always report `hasOverlay: false`. Because the triple-inspection display needs a per-inspection annotated image, a small additive LocalServer/artifact change (for example, persisting and listing an annotated per-node port such as `annotated`, or per-inspection overlay artifacts) is acceptable, following the same additive-change rule the quality-station-hmi spec established: no existing route or response shape may change.

## Glossary

- **Triple_HMI**: The IMTS Triple Inspection HMI — the browser-based display application this document specifies.
- **LocalServer**: The existing FastAPI REST API backend running on the edge device (`src/backend`), reachable over the local network.
- **Target_Workflow**: The Workflow_Registration whose name field equals "blue-plate-detection-guided-inspection", as returned by `GET /workflows/registrations`.
- **Workflow_Registration**: A deployed engine workflow on the device, as returned by `GET /workflows/registrations` (identified by registrationId, with workflowId, name, version, and status).
- **Workflow_Run**: A single execution of a Workflow_Registration, as returned by `GET /workflows/executions/{execution_id}` (identified by executionId, with status, startedAt, finishedAt, and error fields).
- **Inspection**: One of the three per-part inspection results produced by a single Workflow_Run of the Target_Workflow, each associated with one physical plate position and comprising an Original_Image, an Annotated_Image, and, where present, per-inspection verdict fields.
- **Inspection_Slot**: One of the three fixed screen regions of the Triple_HMI, each displaying one Inspection of the displayed Workflow_Run.
- **Original_Image**: The un-annotated captured frame of one Inspection, served via the LocalServer image routes.
- **Annotated_Image**: The inspected image of one Inspection with defect bounding boxes overlaid, where the boxes are the `bounding_box` entries of the Defect_Objects returned in the Target_Workflow's Bedrock inspection response for that Inspection, served via the LocalServer image routes.
- **Defect_Object**: One entry of the `objects` list in a Bedrock inspection response, carrying `name`, `qc` ("OK" or "NOK"), `reason`, and `bounding_box` (`x_min`, `y_min`, `x_max`, `y_max`) in the coordinate space of the image sent to Bedrock.
- **Run_Result**: The per-run data the Triple_HMI displays: the run's status, the three Inspections' images, and each Inspection's verdict fields.
- **Inspection_Verdict**: The inspection outcome fields available for a run or an individual Inspection from the run's metadata: `is_anomalous` and `confidence`, and any per-inspection equivalents the Target_Workflow produces.
- **Session_Token**: The bearer token issued by `POST /local-auth/login`, required by authenticated LocalServer routes and passed as a `token` query parameter on image routes.
- **Operator**: The person at the quality station watching the Triple_HMI screen.
- **Kiosk_Display**: The full-screen 1920x1080 browser presentation mode of the Triple_HMI.
- **Run_Detection_Latency**: The elapsed time between a Workflow_Run reaching a terminal status on the LocalServer and the Triple_HMI beginning to display that run.
- **Live_View**: The Triple_HMI screen region that displays the most recent Run_Result, composed of the three Inspection_Slots plus run-level status.

## Requirements

### Requirement 1: Authentication and Session Handling

**User Story:** As an Operator, I want the Triple_HMI to authenticate against the quality station's local server the same way the existing HMI does, so that the display can access workflow and image data on a device with authentication enabled.

#### Acceptance Criteria

1. WHEN the Triple_HMI starts on a device where the LocalServer reports local login enabled, and there is either no stored Session_Token or a stored Session_Token whose `expiresAt` time is at or before the current time, THE Triple_HMI SHALL present a login form that submits the entered username and password to `POST /local-auth/login`.
2. WHEN `POST /local-auth/login` succeeds and returns a Session_Token, THE Triple_HMI SHALL store the Session_Token together with its `expiresAt` value and attach the Session_Token as a bearer credential in the `Authorization` header of all subsequent authenticated non-image LocalServer requests.
3. WHEN the Triple_HMI requests an image route, THE Triple_HMI SHALL pass the Session_Token as the `token` query parameter, matching the LocalServer's token-in-query image serving.
4. IF an authenticated LocalServer request other than `POST /local-auth/login` returns HTTP 401, THEN THE Triple_HMI SHALL attempt re-authentication exactly once by resubmitting to `POST /local-auth/login` the credentials retained in memory from the most recent successful login; IF the re-authentication attempt succeeds, THEN THE Triple_HMI SHALL retry the original failed request exactly once with the newly issued Session_Token; IF the single re-authentication attempt fails or no credentials are retained, THEN THE Triple_HMI SHALL discard the stored Session_Token and present the login form.
5. WHEN a stored Session_Token exists at startup with an `expiresAt` time later than the current time, THE Triple_HMI SHALL resume operation without prompting for credentials.
6. IF `POST /local-auth/login` returns HTTP 403 indicating local login is disabled, THEN THE Triple_HMI SHALL display a message stating that local login is disabled on the device.
7. IF `POST /local-auth/login` returns HTTP 401, THEN THE Triple_HMI SHALL display an error message indicating that the credentials were rejected, SHALL NOT store a Session_Token, and SHALL keep the login form displayed for credential re-entry.
8. WHEN the Triple_HMI starts, THE Triple_HMI SHALL determine whether local login is enabled by querying `GET /local-auth/status`; IF the response indicates local login is disabled, THEN THE Triple_HMI SHALL resume operation without presenting a login form.
9. IF a request to `POST /local-auth/login` receives no response within 10 seconds or fails with a network error, THEN THE Triple_HMI SHALL display an error message indicating the LocalServer is unreachable, SHALL NOT store a Session_Token, and SHALL keep the login form displayed for retry.

### Requirement 2: Target Workflow Binding

**User Story:** As an Operator, I want the Triple_HMI to automatically attach to the "blue-plate-detection-guided-inspection" workflow, so that the station display always shows the triple inspection without manual selection.

#### Acceptance Criteria

1. WHEN the Triple_HMI obtains a valid Session_Token (via login or startup resume), THE Triple_HMI SHALL retrieve the device's Workflow_Registrations from `GET /workflows/registrations`.
2. WHEN the retrieved Workflow_Registrations include exactly one registration whose status field indicates it is active and whose name field is a case-sensitive exact string match for "blue-plate-detection-guided-inspection", THE Triple_HMI SHALL select that registration as the Target_Workflow and display its Live_View within 2 seconds of receiving the registrations response.
3. IF more than one active Workflow_Registration has the name "blue-plate-detection-guided-inspection", THEN THE Triple_HMI SHALL select as the Target_Workflow the matching registration with the most recent `registeredAt` value; IF two or more matching registrations have equal `registeredAt` values or lack a `registeredAt` value, THEN THE Triple_HMI SHALL select the first such registration in the order returned by `GET /workflows/registrations`.
4. IF no active Workflow_Registration named "blue-plate-detection-guided-inspection" exists, THEN THE Triple_HMI SHALL display a message stating that the blue-plate-detection-guided-inspection workflow is not deployed on the device, and SHALL re-check registrations on the connection retry cycle defined in Requirement 8; WHEN a re-check returns a matching active registration, THE Triple_HMI SHALL bind to it according to criteria 2 and 3 without Operator interaction.
5. WHERE the Target_Workflow name is configurable, THE Triple_HMI SHALL read the configured workflow name from a build-time or query-parameter configuration value that defaults to "blue-plate-detection-guided-inspection", SHALL use the query-parameter value when both a build-time value and a query-parameter value are present, SHALL use the default when the configured value is empty or contains only whitespace, and SHALL apply all Target_Workflow binding behavior to the configured name.
6. IF the Target_Workflow has no Workflow_Runs, THEN THE Triple_HMI SHALL display the Live_View with the Target_Workflow's name and a message stating that no runs have been recorded, in place of Run_Result content.
7. IF a subsequent successful `GET /workflows/registrations` response shows the Target_Workflow as inactive or absent, THEN THE Triple_HMI SHALL display the not-deployed message defined in criterion 4 in place of the Live_View and SHALL resume the re-check behavior defined in criterion 4.

### Requirement 3: Real-Time Run Updates

**User Story:** As an Operator, I want the display to update every time the workflow runs, so that I can watch each triple inspection happen live.

#### Acceptance Criteria

1. WHILE the Triple_HMI is displaying a Live_View, THE Triple_HMI SHALL poll the Target_Workflow's recent executions via `GET /workflows/registrations/{registration_id}/executions?limit=N` with N set to 10, on an update cycle with a period of at most 2 seconds, such that the Run_Detection_Latency for any Workflow_Run reaching a terminal status is at most 2 seconds.
2. WHEN the Triple_HMI detects a Workflow_Run of the Target_Workflow that has reached a terminal status (completed or failed) and is more recent than the currently displayed Workflow_Run according to the ordering defined in criterion 3, THE Triple_HMI SHALL update the Live_View to display that run's Run_Result across all three Inspection_Slots.
3. WHEN more than one Workflow_Run reaches a terminal status between two successive update cycles, THE Triple_HMI SHALL display the terminal Workflow_Run with the most recent `finishedAt` timestamp, using `startedAt` as the ordering key when `finishedAt` values are equal or absent.
4. WHILE a Workflow_Run of the Target_Workflow is in progress (pending or running status), THE Triple_HMI SHALL display an in-progress indicator within 2 seconds of detecting the in-progress status, without removing the currently displayed Run_Result, and SHALL remove the in-progress indicator within 2 seconds of detecting that no Workflow_Run of the Target_Workflow remains in progress.
5. WHEN a Live_View is first displayed and the Target_Workflow has at least one terminal Workflow_Run, THE Triple_HMI SHALL display the Run_Result of the most recent terminal Workflow_Run, ordered as defined in criterion 3.
6. WHEN the Live_View updates to a new Workflow_Run, THE Triple_HMI SHALL replace the previously displayed Run_Result in all three Inspection_Slots within the same view without requiring Operator interaction.
7. IF a Live_View is first displayed and the Target_Workflow has no terminal Workflow_Runs, THEN THE Triple_HMI SHALL display a placeholder state in all three Inspection_Slots indicating that no completed runs exist, and SHALL continue polling per criterion 1.
8. IF a polling request fails (network error, timeout, or non-success response), THEN THE Triple_HMI SHALL retain the currently displayed content unchanged and SHALL retry on the next update cycle.
9. IF polling requests fail for 5 or more consecutive update cycles, THEN THE Triple_HMI SHALL display an indicator that the Live_View data may be stale, and SHALL remove that indicator within one update cycle after a polling request succeeds.

### Requirement 4: Per-Inspection Image Data

**User Story:** As an Operator, I want each of the three inspections to provide both the bounding-box annotated image and the original image, so that the display can show what the model found next to what the camera saw.

#### Acceptance Criteria

1. WHEN a Workflow_Run of the Target_Workflow reaches status `completed`, THE Triple_HMI SHALL retrieve the run's image inventory from `GET /workflows/executions/{execution_id}/results` and the run's metadata from `GET /workflows/executions/{execution_id}/metadata`.
2. THE Triple_HMI SHALL map the run's image inventory entries to exactly three Inspections by grouping inventory entries that share the same `nodeId` into one Inspection, associating each Inspection with one Original_Image entry and one Annotated_Image entry, and ordering the Inspections by lexicographic ascending `nodeId` and, within a `nodeId`, ordering entries by lexicographic ascending `port`, such that two independent evaluations of the same inventory derive identical Inspection lists.
3. THE Triple_HMI SHALL assign Inspections to Inspection_Slots based solely on the sorted inventory keys (`nodeId` ascending, then `port` ascending) of the displayed run, such that the same `nodeId` always appears in the same Inspection_Slot across runs having identical inventory keys.
4. WHERE the existing LocalServer results inventory and image routes cannot supply a per-Inspection Annotated_Image (the current backend persists only one run-level overlay artifact and lists node entries with `hasOverlay: false`), a small additive LocalServer or run-artifact change (for example, persisting per-node annotated frames — produced by rendering the Defect_Object `bounding_box` entries from the Bedrock inspection response onto the Inspection's Original_Image — under an additional port and listing them in the existing `/results` response) MAY be introduced, and such a change SHALL leave all existing routes and response shapes unchanged.
5. WHEN loading Inspection images, THE Triple_HMI SHALL request them from the LocalServer image routes with the Session_Token as a query parameter, applying a timeout of 10 seconds per image request.
6. IF the run's image inventory yields fewer than three Inspections, THEN THE Triple_HMI SHALL display the Inspections that exist in their assigned Inspection_Slots and SHALL display in each remaining Inspection_Slot a placeholder stating that no inspection data is available for that slot.
7. IF the run's image inventory yields more than three Inspections, THEN THE Triple_HMI SHALL display the first three Inspections in the deterministic inventory order and SHALL display an indicator that additional inspection images exist.
8. IF the metadata request for a Workflow_Run fails, THEN THE Triple_HMI SHALL retry the request once; IF the retry also fails, THEN THE Triple_HMI SHALL display the run's images and status with an indication that verdict data is unavailable.
9. IF the request to `GET /workflows/executions/{execution_id}/results` fails or does not complete within 10 seconds, THEN THE Triple_HMI SHALL retry the request once; IF the retry also fails, THEN THE Triple_HMI SHALL display in each Inspection_Slot a placeholder indicating that inspection data is unavailable, while continuing to display the run's status.
10. IF an Inspection has no Annotated_Image entry in the run's image inventory (for example, node entries report `hasOverlay: false` and no annotated port exists), THEN THE Triple_HMI SHALL display the Inspection's Original_Image and SHALL display in the annotated-image position a placeholder indicating that no annotated image is available, without substituting any other image.
11. IF an individual image request fails or does not complete within the 10-second timeout, THEN THE Triple_HMI SHALL display a placeholder in that image panel only, without substituting an image from a different Inspection, port, or run.
12. WHEN producing an Inspection's Annotated_Image, THE producing component SHALL render each Defect_Object's `bounding_box` from that Inspection's Bedrock inspection response onto that Inspection's Original_Image, clamping each `bounding_box` to the bounds of the Original_Image, and SHALL exclude from rendering any Defect_Object whose `bounding_box` is missing, malformed, or empty after clamping; IF the Bedrock inspection response for an Inspection contains no parseable `objects` list, THEN no Annotated_Image SHALL be produced for that Inspection, and THE Triple_HMI SHALL apply the missing-Annotated_Image behavior defined in criterion 10.

### Requirement 5: Triple Inspection Display

**User Story:** As an Operator, I want to see all three inspections at once, each showing the annotated image next to the original image, so that I can assess the whole run cycle at a glance.

#### Acceptance Criteria

1. WHEN a Workflow_Run's Run_Result is displayed, THE Triple_HMI SHALL display three Inspection_Slots simultaneously in the Live_View, each showing one Inspection of the displayed run.
2. WHILE a Run_Result is displayed, THE Triple_HMI SHALL display within each Inspection_Slot the Inspection's Annotated_Image and Original_Image side by side, with each image visibly labeled to identify which image is annotated and which is the original.
3. WHILE a Run_Result is displayed, THE Triple_HMI SHALL display the two images within each Inspection_Slot at equal display heights, preserving each image's aspect ratio, with each image fully visible (uncropped).
4. THE Triple_HMI SHALL label each Inspection_Slot with a slot identifier (for example, a position number 1 to 3) derived from the inventory-key slot assignment defined in Requirement 4, such that the identifier for a given `nodeId` persists across runs.
5. WHERE the run metadata contains a per-Inspection `is_anomalous` equivalent for an Inspection and the value is a boolean, THE Triple_HMI SHALL display in that Inspection_Slot a fail verdict when the value is true and a pass verdict when the value is false, rendering each verdict with a textual label whose text is distinct between the pass and fail states and with styling that differentiates the two states by more than color alone.
6. WHERE the run metadata contains only run-level Inspection_Verdict fields (`is_anomalous`, `confidence`) without per-Inspection values, THE Triple_HMI SHALL display the run-level verdict once at the run level of the Live_View rather than duplicating it into the three Inspection_Slots.
7. WHERE the run metadata contains a `confidence` value for a displayed verdict, THE Triple_HMI SHALL display the confidence value rounded to exactly 2 decimal places alongside that verdict.
8. IF an image request for an Inspection returns an error or does not complete within the 10-second timeout, THEN THE Triple_HMI SHALL display in the affected image panel a placeholder indicating the image is unavailable, without substituting an image from a different Inspection, port, or run, while continuing to display the run's other content.
9. WHEN a Workflow_Run has status `failed`, THE Triple_HMI SHALL display a run-level failure state including an error summary sourced from that run's error fields as returned by `GET /workflows/executions/{execution_id}`; IF the error fields are empty or absent, THEN THE Triple_HMI SHALL display the failure state with a message indicating that no error details are available; WHILE the failed Workflow_Run is displayed, THE Triple_HMI SHALL display a placeholder in each of the three Inspection_Slots and SHALL exclude images from prior Workflow_Runs from the Inspection_Slots.
10. IF the metadata route returns an object lacking all Inspection_Verdict fields for a Workflow_Run with status `completed`, THEN THE Triple_HMI SHALL display the run's images and status without verdict content rather than displaying an error.
11. WHERE the run metadata contains both per-Inspection verdict fields and run-level Inspection_Verdict fields, THE Triple_HMI SHALL display the per-Inspection verdicts in the Inspection_Slots and the run-level verdict at the run level of the Live_View, without conflating the two.
12. IF per-Inspection verdict values are present for only some Inspections or a per-Inspection verdict value is non-boolean, THEN THE Triple_HMI SHALL display a no-verdict indication in each affected Inspection_Slot while displaying valid verdicts in the other Inspection_Slots.

### Requirement 6: Kiosk Display Layout

**User Story:** As an Operator, I want a full-screen layout designed for three simultaneous inspections of small rectangular plates on the station's 1920x1080 monitor, so that I can read all three results at a glance from working distance.

#### Acceptance Criteria

1. THE Triple_HMI SHALL lay out the Kiosk_Display for a 1920x1080 viewport at 100% browser zoom with all primary Live_View content (three Inspection_Slots each containing two labeled images, verdict content, workflow identity, and run timing) simultaneously visible without scrolling and without content overlapping.
2. THE Triple_HMI SHALL size the three Inspection_Slots such that their rendered widths are equal within 2 pixels, and SHALL proportion each slot's image panels for the inspected plates' 1:2 (1.5 inch by 3 inch) form factor such that each image panel's width-to-height ratio is between 1:1.8 and 1:2.2.
3. WHILE a Live_View is shown, THE Triple_HMI SHALL display the Target_Workflow's name and the displayed Workflow_Run's `startedAt` time, and where present its `finishedAt` time, rendered in the local time zone with at least seconds precision.
4. WHERE a verdict is displayed (per-Inspection or run-level), THE Triple_HMI SHALL render the verdict text with a minimum rendered text height of 32 pixels at a 1920x1080 viewport, applied to every verdict state rendered as text.
5. IF the browser viewport dimensions differ from 1920x1080, THEN THE Triple_HMI SHALL keep all primary Live_View content visible without horizontal scrolling and without content overlapping for viewport widths between 1280 and 1920 pixels.
6. THE Triple_HMI SHALL operate as a browser application requiring no installation on the device beyond serving static assets, served through the LocalServer's static-mount pattern used by the existing HMI.
7. THE Triple_HMI SHALL function in a Chromium-based browser running in full-screen kiosk mode, with all Kiosk_Display content rendered within the browser viewport and no function dependent on browser chrome.
8. THE Triple_HMI SHALL render each displayed image preserving its source aspect ratio, uncropped, with a minimum rendered width of 280 pixels at a 1920x1080 viewport.
9. IF a displayed Workflow_Run has no `finishedAt` value, THEN THE Triple_HMI SHALL omit the finish time from the run timing display without displaying an error or placeholder.

### Requirement 7: Run History Strip

**User Story:** As an Operator, I want to see the outcomes of recent triple-inspection runs, so that I can spot failure patterns without leaving the live display.

#### Acceptance Criteria

1. WHILE a Live_View is displayed, THE Triple_HMI SHALL display a history summary of the most recent Workflow_Runs of the Target_Workflow, ordered newest first, showing for each run its verdict state and its start time, with a display capacity of at least the 5 most recent runs, where the verdict state is determined in this precedence order: failed-run when the Workflow_Run reached a failed terminal status, fail when at least one of the run's three Inspections has a failing verdict, no-verdict when the run reached a terminal status with verdict data absent for one or more Inspections, and pass when all three Inspections have passing verdicts.
2. WHEN a new Workflow_Run reaches a terminal status, THE Triple_HMI SHALL add that run at the newest position of the history summary within the Run_Detection_Latency bound defined in Requirement 3; IF adding the run exceeds the displayed capacity, THEN THE Triple_HMI SHALL remove the oldest entry from the history summary.
3. WHEN an Operator selects a run from the history summary, THE Triple_HMI SHALL, within 2 seconds of the selection, display that run's full three-Inspection Run_Result in the Live_View, display a visible indicator that the view is showing a historical run, and display a control for returning to live mode.
4. WHILE the Live_View is showing a historical run, WHEN a new Workflow_Run reaches a terminal status, THE Triple_HMI SHALL update the history summary and display an indication that a newer run is available, both within the Run_Detection_Latency bound defined in Requirement 3, without replacing the historical view.
5. WHEN an Operator activates the return-to-live control, THE Triple_HMI SHALL remove the historical-run indicator and resume automatic display of the most recent Workflow_Run according to the real-time update behavior defined in Requirement 3.
6. IF the Target_Workflow has fewer Workflow_Runs than the displayed capacity, THEN THE Triple_HMI SHALL display history entries only for the runs that exist; IF zero Workflow_Runs exist, THEN THE Triple_HMI SHALL display the history summary area with a message indicating that no run history is available.
7. IF a selected historical run's Run_Result data is unavailable from the LocalServer, THEN THE Triple_HMI SHALL display an error indication in the Live_View, retain the history summary, and keep the return-to-live control available.
8. WHEN a Live_View is first displayed for the Target_Workflow, THE Triple_HMI SHALL populate the history summary from the Target_Workflow's existing Workflow_Runs retrieved from the LocalServer, ordered newest first, up to the displayed capacity.

### Requirement 8: Connection Resilience

**User Story:** As an Operator, I want the display to recover on its own from network or server interruptions, so that the kiosk keeps working unattended.

#### Acceptance Criteria

1. IF a LocalServer request fails with a network error, receives no response within 10 seconds, or receives an HTTP 5xx response (excluding HTTP 401 responses, which are handled per Requirement 1), THEN THE Triple_HMI SHALL, within 1 second of detecting the failure, display a connection status indicator showing the disconnected state while retaining the last successfully displayed Run_Result together with the time of the last successful LocalServer update.
2. WHILE disconnected from the LocalServer, THE Triple_HMI SHALL issue a retry request to `GET /workflows/registrations` at an interval of at most 10 seconds, SHALL continue retrying with no upper limit on the number of attempts until connectivity is restored, and SHALL treat connectivity as restored when a retry request receives an HTTP 2xx response.
3. WHEN connectivity to the LocalServer is restored, THE Triple_HMI SHALL, without Operator interaction, update the connection status indicator to the connected state within 1 second and resume the real-time update cycle defined in Requirement 3.
4. WHILE connected to the LocalServer, THE Triple_HMI SHALL display a connection status indicator showing the connected state and SHALL continue refreshing Live_View data on the real-time update cycle defined in Requirement 3.
5. IF a successful `GET /workflows/registrations` response shows the Target_Workflow as inactive or absent, THEN THE Triple_HMI SHALL display the not-deployed message defined in Requirement 2 within 2 seconds of receiving that response, while continuing to re-check registrations on the retry cycle defined in criterion 2.
6. WHEN connectivity to the LocalServer is restored, THE Triple_HMI SHALL refresh the Live_View with the most recent completed Workflow_Run of the Target_Workflow within 5 seconds, regardless of whether any new Workflow_Runs completed during the disconnected period.
7. WHEN connectivity to the LocalServer is restored, THE Triple_HMI SHALL refresh the run history summary defined in Requirement 7 within 5 seconds to include Workflow_Runs that completed during the disconnected period, up to the display capacity defined in Requirement 7.
8. WHILE the not-deployed message defined in Requirement 2 is displayed, WHEN a successful `GET /workflows/registrations` response shows the Target_Workflow as active, THE Triple_HMI SHALL resume displaying the Live_View of the Target_Workflow within 2 seconds without Operator interaction.
