# Requirements Document

## Introduction

The Quality Station HMI is a browser-based, external human-machine interface application that runs full screen on a 1920x1080 monitor attached to a quality station (edge device). It lets operators actively watch inspections happen: every time a workflow runs on the device, the display updates with the run's captured frame, inspection verdict, and — for workflows with a Bedrock or VLM reference-comparison node — the configured reference image shown side by side with the captured frame.

The HMI is a separate application that consumes the device's existing LocalServer REST API (the FastAPI backend in `src/backend`). It works generically across engine workflows, with "IMTS - Swagfactory" (llm_inference / VLM node) and "IMTS Stagfactory (Bedrock)" (bedrock_inference node) as the driving use cases. It does not require changes to core workflow execution; small additive API changes are acceptable where the existing surface is insufficient (for example, an efficient way to detect new runs).

The existing API surface this feature builds on:

- `POST /local-auth/login` and `GET /local-auth/status` — token-based local authentication.
- `GET /workflows/registrations` and `GET /workflows/registrations/{id}` — engine workflow registrations with their executions.
- `GET /workflows/executions/{id}` — run status; `/results` — on-disk image inventory (output entry plus per-node `{nodeId, port}` entries, including the `reference` port persisted by reference-comparison nodes); `/metadata` — the run's final tag values including `generated_text`, `is_anomalous`, and `confidence`; `/node-image?nodeId=&port=` and `/output-image` — image bytes served with token-in-query for browser `<img>` loads.

## Glossary

- **HMI**: The Quality Station HMI — the browser-based display application this document specifies.
- **LocalServer**: The existing FastAPI REST API backend running on the edge device (`src/backend`), reachable over the local network.
- **Workflow_Registration**: A deployed engine workflow on the device, as returned by `GET /workflows/registrations` (identified by registrationId, with workflowId, name, version, and status).
- **Workflow_Run**: A single execution of a Workflow_Registration, as returned by `GET /workflows/executions/{execution_id}` (identified by executionId, with status, startedAt, finishedAt, and error fields).
- **Run_Result**: The per-run data the HMI displays: the run's status, captured images, and Inspection_Verdict.
- **Inspection_Verdict**: The inspection outcome fields from a run's metadata: `is_anomalous`, `confidence`, and/or `generated_text`, depending on the node types in the workflow.
- **Reference_Image**: The configured reference frame persisted by a reference-comparison node (llm_inference or bedrock_inference) on its `reference` port, served via `GET /workflows/executions/{execution_id}/node-image?nodeId=&port=reference`.
- **Captured_Frame**: The frame captured for an inspection run: the node `in` port image when present, otherwise the run's base output image.
- **Session_Token**: The bearer token issued by `POST /local-auth/login`, required by authenticated LocalServer routes and passed as a `token` query parameter on image routes.
- **Operator**: The person at the quality station watching the HMI screen.
- **Kiosk_Display**: The full-screen 1920x1080 browser presentation mode of the HMI.
- **Run_Detection_Latency**: The elapsed time between a Workflow_Run reaching a terminal status on the LocalServer and the HMI beginning to display that run.
- **Live_View**: The HMI screen region that displays the most recent Run_Result for a workflow.

## Requirements

### Requirement 1: Authentication

**User Story:** As an Operator, I want the HMI to authenticate against the quality station's local server, so that the display can access workflow and image data on a device with authentication enabled.

#### Acceptance Criteria

1. WHEN the HMI starts without a stored Session_Token, or with a stored Session_Token whose `expiresAt` time is at or before the current time, THE HMI SHALL present a login form that submits the entered username and password to `POST /local-auth/login`.
2. WHEN `POST /local-auth/login` succeeds and returns a Session_Token, THE HMI SHALL store the Session_Token together with its `expiresAt` value and attach the Session_Token as a bearer credential in the `Authorization` header of all subsequent authenticated non-image LocalServer requests.
3. WHEN the HMI requests an image route (`/output-image` or `/node-image`), THE HMI SHALL pass the Session_Token as the `token` query parameter, matching the LocalServer's token-in-query image serving.
4. IF an authenticated LocalServer request other than `POST /local-auth/login` returns HTTP 401, THEN THE HMI SHALL attempt re-authentication exactly once by resubmitting to `POST /local-auth/login` the credentials retained in memory from the most recent successful login.
5. WHEN a stored Session_Token exists at startup with an `expiresAt` time later than the current time, THE HMI SHALL resume operation without prompting for credentials.
6. IF `POST /local-auth/login` returns HTTP 403 indicating local login is disabled, THEN THE HMI SHALL display a message stating that local login is disabled on the device.
7. IF `POST /local-auth/login` returns HTTP 401, THEN THE HMI SHALL display an error message indicating that the credentials were rejected, SHALL NOT store a Session_Token, and SHALL keep the login form displayed for credential re-entry.
8. IF the single re-authentication attempt fails, or no credentials are retained in memory, THEN THE HMI SHALL discard the stored Session_Token and present the login form.

### Requirement 2: Workflow Discovery and Selection

**User Story:** As an Operator, I want the HMI to show the workflows deployed on the quality station, so that I can watch the inspections that matter for my station.

#### Acceptance Criteria

1. WHEN the HMI obtains a valid Session_Token (via login or startup resume), THE HMI SHALL retrieve the device's Workflow_Registrations from `GET /workflows/registrations`.
2. THE HMI SHALL display each Workflow_Registration whose status field indicates it is active, labeled with its name field when that field is present and non-empty and with its workflowId otherwise, and SHALL exclude registrations with a non-active status from the selection list.
3. WHEN an Operator selects a Workflow_Registration, THE HMI SHALL display that registration's Live_View within 2 seconds of the selection, replacing any previously displayed Live_View.
4. WHERE one or more active Workflow_Registrations exist and the Operator has not made a selection, THE HMI SHALL display the Live_View of the active Workflow_Registration whose most recent Workflow_Run has the latest startedAt timestamp.
5. IF `GET /workflows/registrations` returns zero active Workflow_Registrations, THEN THE HMI SHALL display a message stating that no workflows are deployed on the device, and SHALL re-check registrations on the connection retry cycle defined in Requirement 8.
6. THE HMI SHALL derive workflow labels and display behavior solely from fields returned by the LocalServer API, without conditional logic keyed to specific workflow names or workflowIds.
7. IF no active Workflow_Registration has any Workflow_Run at the time the default selection in criterion 4 is made, THEN THE HMI SHALL display the Live_View of the first active Workflow_Registration in the order returned by `GET /workflows/registrations`.
8. IF the displayed Workflow_Registration has no Workflow_Runs, THEN THE HMI SHALL display its Live_View with a message stating that no runs have been recorded, in place of Run_Result content.

### Requirement 3: Real-Time Run Updates

**User Story:** As an Operator, I want the display to update every time a workflow runs, so that I can watch inspections happen live.

#### Acceptance Criteria

1. WHILE the HMI is displaying a Live_View, THE HMI SHALL check for or receive Workflow_Run status updates for the displayed Workflow_Registration on an update cycle with a period of at most 2 seconds, such that the Run_Detection_Latency for any Workflow_Run reaching a terminal status is at most 2 seconds.
2. WHEN the HMI detects a Workflow_Run of the displayed Workflow_Registration that has reached a terminal status (completed or failed) and is more recent than the currently displayed Workflow_Run, THE HMI SHALL update the Live_View to display that run's Run_Result.
3. WHILE a Workflow_Run of the displayed Workflow_Registration is in progress (pending or running status), THE HMI SHALL display an in-progress indicator for that run within 2 seconds of detecting the in-progress status, without removing the currently displayed Run_Result.
4. WHEN more than one Workflow_Run reaches a terminal status between two successive update cycles, THE HMI SHALL display the terminal Workflow_Run with the most recent finishedAt timestamp, using startedAt as the ordering key when finishedAt values are equal or absent.
5. WHEN the Live_View updates to a new Workflow_Run, THE HMI SHALL replace the previously displayed Run_Result within the same view without requiring Operator interaction.
6. THE HMI SHALL detect new Workflow_Runs using LocalServer API mechanisms; WHERE the existing API surface cannot meet the Run_Detection_Latency bound without exceeding 1 HTTP request per second per displayed Workflow_Registration or without response payloads that grow proportionally with the device's total run history, a small additive LocalServer API change (for example, a latest-execution query or an event stream) MAY be introduced, and such a change SHALL leave all existing routes and response shapes unchanged.
7. WHEN a Live_View is first displayed for a Workflow_Registration that has at least one terminal Workflow_Run, THE HMI SHALL display the Run_Result of the most recent terminal Workflow_Run, ordered as defined in criterion 4.
8. IF a Live_View is displayed for a Workflow_Registration that has no Workflow_Runs, THEN THE HMI SHALL display a message indicating that no runs exist yet and SHALL continue the update cycle defined in criterion 1.

### Requirement 4: Inspection Result Display

**User Story:** As an Operator, I want to see each run's inspection verdict, so that I know immediately whether the inspected part passed or failed.

#### Acceptance Criteria

1. WHEN a Workflow_Run reaches status `completed`, THE HMI SHALL retrieve the run's image inventory from `GET /workflows/executions/{execution_id}/results` and the run's metadata from `GET /workflows/executions/{execution_id}/metadata`.
2. WHERE the run metadata contains an `is_anomalous` field, THE HMI SHALL display a fail verdict when `is_anomalous` is true and a pass verdict when `is_anomalous` is false, rendering each verdict with a textual label and with styling that differentiates the two states by more than color alone.
3. WHERE the run metadata contains a `confidence` field with a numeric value, THE HMI SHALL display the confidence value rounded to at most 2 decimal places.
4. WHERE the run metadata contains a `generated_text` field, THE HMI SHALL display up to the first 500 characters of the generated text and SHALL display a visible truncation indicator when the text exceeds 500 characters.
5. WHEN a Workflow_Run has status `failed`, THE HMI SHALL display a failure state including an error summary sourced from that run's error fields as returned by `GET /workflows/executions/{execution_id}`.
6. THE HMI SHALL display the run's `startedAt` time and, where present, its `finishedAt` time for the displayed Workflow_Run, rendered in the local time zone with at least seconds precision.
7. IF the metadata route returns an empty object, or an object lacking all of `is_anomalous`, `confidence`, and `generated_text`, for a Workflow_Run with status `completed`, THEN THE HMI SHALL display the run's images and status without a verdict panel rather than displaying an error.
8. IF a Workflow_Run has status `failed` and its error fields are empty or absent, THEN THE HMI SHALL display the failure state with a message indicating that no error details are available.
9. IF the metadata request for a Workflow_Run fails, THEN THE HMI SHALL retry the request once; IF the retry also fails, THEN THE HMI SHALL display the run's images and status with an indication that verdict data is unavailable.

### Requirement 5: Reference and Captured Image Display

**User Story:** As an Operator, I want to see the configured reference image side by side with the frame captured for each inspection, so that I can visually compare what the model compared.

#### Acceptance Criteria

1. WHEN a displayed Workflow_Run's results include a node image entry with port `reference`, THE HMI SHALL display the Reference_Image and the Captured_Frame side by side in the Live_View, with each panel visibly labeled to identify which image is the reference and which is the captured frame.
2. WHEN a displayed Workflow_Run's results include one or more node image entries with port `in`, THE HMI SHALL use as the Captured_Frame the `in` entry from the same node as the displayed `reference` entry when such an entry exists, and otherwise the first `in` entry in the order listed in the results response.
3. WHEN a displayed Workflow_Run's results include an `output` image entry and no node `in` entry, THE HMI SHALL use the base output image as the Captured_Frame.
4. WHERE a Workflow_Run's results include no `reference` entry, THE HMI SHALL display the Captured_Frame without a reference panel, using the reclaimed space for the Captured_Frame.
5. WHEN loading run images, THE HMI SHALL request them from the LocalServer image routes (`/output-image`, `/node-image?nodeId=&port=`) with the Session_Token as a query parameter, applying a timeout of 10 seconds per image request.
6. IF an image request returns an error or does not complete within the 10-second timeout, or a run has no viewable images, THEN THE HMI SHALL display in the affected image panel a placeholder indicating the image is unavailable, without substituting an image from a different port or run, while continuing to display the run's other Run_Result data.
7. WHERE a run's results include node images from multiple nodes, THE HMI SHALL display the image pair of exactly one node, selecting the first listed node that has a `reference` entry when one exists and otherwise the first listed node, and SHALL indicate that additional node images exist.

### Requirement 6: Kiosk Display Layout

**User Story:** As an Operator, I want a full-screen display designed for the station's 1920x1080 monitor, so that I can read inspection results at a glance from working distance.

#### Acceptance Criteria

1. THE HMI SHALL lay out the Kiosk_Display for a 1920x1080 viewport at 100% browser zoom with all primary Live_View content (verdict, reference image, captured frame, workflow identity) simultaneously visible without scrolling and without content overlapping.
2. THE HMI SHALL render the anomaly verdict text with a minimum rendered text height of 48 pixels at a 1920x1080 viewport, applied to both the anomalous and normal verdict states.
3. WHILE a Live_View is shown, THE HMI SHALL display the currently selected Workflow_Registration's name and the displayed Workflow_Run's start time on the Kiosk_Display.
4. IF the selected Workflow_Registration has no Workflow_Runs, THEN THE HMI SHALL display the Workflow_Registration's name and a message indicating that no runs are available, in place of Run_Result content.
5. WHEN displaying the Reference_Image and Captured_Frame side by side, THE HMI SHALL scale both images to equal display heights while preserving each image's aspect ratio, with each image fully visible (uncropped) within the Kiosk_Display.
6. IF the browser viewport dimensions differ from 1920x1080, THEN THE HMI SHALL keep all primary Live_View content visible without horizontal scrolling for viewport widths between 1280 and 1920 pixels.
7. THE HMI SHALL operate as a browser application requiring no installation on the device beyond serving static assets.
8. THE HMI SHALL function in a Chromium-based browser running in full-screen kiosk mode, with all Kiosk_Display content rendered within the browser viewport and no HMI function dependent on browser chrome (address bar, tabs, or menus).

### Requirement 7: Run History Strip

**User Story:** As an Operator, I want to see the outcomes of recent runs, so that I can spot failure patterns without leaving the live display.

#### Acceptance Criteria

1. WHILE a Live_View is displayed, THE HMI SHALL display a history summary of the most recent Workflow_Runs of the displayed Workflow_Registration, ordered newest first, showing for each run its verdict state (normal, anomalous, failed, or no-verdict) and its start time, with a display capacity of at least the 5 most recent runs.
2. WHEN a new Workflow_Run reaches a terminal status, THE HMI SHALL add that run at the newest position of the history summary within the Run_Detection_Latency bound defined in Requirement 3; IF adding the run exceeds the displayed capacity, THEN THE HMI SHALL remove the oldest entry from the history summary.
3. WHEN an Operator selects a run from the history summary, THE HMI SHALL display that run's Run_Result in the Live_View, display a visible indicator that the view is showing a historical run, and display a control for returning to live mode.
4. WHILE the Live_View is showing a historical run, WHEN a new Workflow_Run completes, THE HMI SHALL update the history summary and indicate that a newer run is available without replacing the historical view.
5. WHEN an Operator activates the return-to-live control, THE HMI SHALL remove the historical-run indicator and resume automatic display of the most recent Workflow_Run according to the real-time update behavior defined in Requirement 3.
6. WHEN a Live_View opens for a Workflow_Registration, THE HMI SHALL populate the history summary from that registration's existing Workflow_Runs retrieved from the LocalServer, up to the displayed capacity.
7. IF the displayed Workflow_Registration has fewer than 5 Workflow_Runs, THEN THE HMI SHALL display history entries only for the runs that exist; IF zero Workflow_Runs exist, THEN THE HMI SHALL display the history summary area with a message indicating that no run history is available.
8. IF a selected historical run's Run_Result data is unavailable from the LocalServer, THEN THE HMI SHALL display an error indication for that run in the Live_View, retain the history summary, and keep the return-to-live control available.

### Requirement 8: Connection Resilience

**User Story:** As an Operator, I want the display to recover on its own from network or server interruptions, so that the kiosk keeps working unattended.

#### Acceptance Criteria

1. IF a LocalServer request fails with a network error, receives no response within 10 seconds, or receives an HTTP 5xx response (excluding HTTP 401 responses, which are handled per Requirement 1), THEN THE HMI SHALL display a connection status indicator showing the disconnected state while retaining the last successfully displayed Run_Result together with the time of the last successful LocalServer update.
2. WHILE disconnected from the LocalServer, THE HMI SHALL issue a retry request to a LocalServer route at an interval of at most 10 seconds, and SHALL treat connectivity as restored when a retry request receives a successful response.
3. WHEN connectivity to the LocalServer is restored, THE HMI SHALL, without Operator interaction, update the connection status indicator to the connected state within 1 second and resume the real-time update cycle defined in Requirement 3.
4. WHILE connected to the LocalServer, THE HMI SHALL display a connection status indicator showing the connected state and SHALL continue refreshing Live_View data on the real-time update cycle defined in Requirement 3.
5. IF a successful `GET /workflows/registrations` response shows the displayed Workflow_Registration as inactive or absent, THEN THE HMI SHALL display a message stating the workflow is no longer available and offer selection of the remaining active Workflow_Registrations; IF zero active Workflow_Registrations remain, THEN THE HMI SHALL display the no-workflows message defined in Requirement 2.
6. WHEN connectivity to the LocalServer is restored, THE HMI SHALL refresh the Live_View with the most recent completed Workflow_Run of the displayed Workflow_Registration within 5 seconds, regardless of whether the underlying data changed during the disconnection.
7. WHEN connectivity to the LocalServer is restored, THE HMI SHALL refresh the run history summary defined in Requirement 7 to include Workflow_Runs that completed during the disconnected period.
