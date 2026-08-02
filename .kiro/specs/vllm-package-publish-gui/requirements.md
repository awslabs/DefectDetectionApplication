# Requirements Document

## Introduction

After a user registers an LLM in the Edge CV Portal (a vLLM_Model_Record, source `vllm`), the model detail page currently offers no way to package and publish the model from the web UI. The Supported Architectures section shows only the placeholder text "Supported architectures are recorded when the model component is packaged and published", and users must call the training package API via curl — which is not viable for users without CLI access.

The backend already implements the complete flow: `POST /api/v1/training/{training_id}/package` dispatches vLLM records to the vLLM packaging branch, and with `auto_triggered: true` chains into the Greengrass publish Lambda, which registers the `model-vllm-{safe_model_name}` component (version `N.0.0`) and writes back the `published_component` map with `supported_architectures`. An explicit re-drive of the publish step exists at `POST /api/v1/training/{training_id}/publish`.

This feature adds a web-GUI action on the model detail page that lets users package and publish a Registered vLLM model end-to-end, observe progress and results, retry after failures, and see the resulting Supported Architectures and component information populate — with no CLI required.

## Glossary

- **Portal**: The Edge CV Portal — the cloud application (Lambda backend, React frontend) that manages models, workflows, devices, and Greengrass deployments.
- **Model_Detail_Page**: The Portal frontend page (`ModelDetail.tsx`) that displays a single model record, including stage, source, Model Information, and — for vLLM records — the Supported Architectures section.
- **vLLM_Model_Record**: A Model Registry record of model type `vllm` (source `vllm`) referencing an LLM by Hugging Face model ID or S3 artifact, registered directly without labeling or training.
- **Package_Publish_Action**: The new Model_Detail_Page control that initiates packaging of a vLLM_Model_Record with automatic chaining into component publish.
- **Packaging_API**: The existing backend operation `POST /api/v1/training/{training_id}/package` that assembles the Triton vLLM repository, uploads the component artifact, and records `packaged_components` on the record; when invoked with `auto_triggered: true` it asynchronously triggers the Publish_API.
- **Publish_API**: The existing backend operation `POST /api/v1/training/{training_id}/publish` that registers the Greengrass component version for a packaged record and writes back the Published_Component map.
- **Packaged_Component_Entry**: An entry in the record's `packaged_components` list produced by the Packaging_API, carrying the target (`jetson-xavier-jp6`), status, artifact location, and `supported_architectures` (for example `["arm64_jp6"]`).
- **Published_Component**: The record's `published_component` write-back map produced by the Publish_API, carrying the component name (`model-vllm-{safe_name}`), component version (`N.0.0`), `supported_architectures`, runtime, and component ARNs.
- **Supported_Architectures_Section**: The Model_Detail_Page section that renders the record's supported Target_Architecture badges from the Published_Component or Packaged_Component_Entry data.
- **Target_Architecture**: A device platform identifier used by packaging and deployment (for this feature primarily `arm64_jp6`; `arm64_jp5` only where JetPack 5 vLLM support is enabled).
- **Vision_Model_Record**: A model record with source `trained` or `imported` whose package and publish actions are already served by the existing CompilationTab UI.

## Requirements

### Requirement 1: Package and Publish Action on the Model Detail Page

**User Story:** As a use case administrator with no CLI access, I want a package-and-publish action on the model detail page for a Registered vLLM model, so that I can produce a deployable Greengrass component entirely from the web UI.

#### Acceptance Criteria

1. WHEN the Model_Detail_Page displays a vLLM_Model_Record that has no Published_Component, THE Portal frontend SHALL display a Package_Publish_Action, and SHALL render the Package_Publish_Action in an enabled state for a signed-in user permitted to package models in the owning use case.
2. WHEN a user activates the Package_Publish_Action and no confirmation is required by criterion 7, THE Portal frontend SHALL invoke the Packaging_API exactly once for the record's training identifier with `auto_triggered` set to true, so that packaging chains into component publish without further user action.
3. WHEN the Model_Detail_Page displays a Vision_Model_Record, THE Portal frontend SHALL present the existing CompilationTab package and publish controls unchanged and SHALL display no Package_Publish_Action for that record.
4. WHEN the Model_Detail_Page displays a vLLM_Model_Record that has a Published_Component, THE Portal frontend SHALL display the Package_Publish_Action labeled as a re-publish operation and SHALL display text stating that re-publishing registers the next component version.
5. WHILE a Packaging_API request initiated from the Package_Publish_Action is in flight, defined as the interval from the user's activation until the Packaging_API returns a success or error response, THE Portal frontend SHALL render the Package_Publish_Action in a loading state and SHALL reject additional activations of the Package_Publish_Action for the same record.
6. WHERE the signed-in user lacks permission to package models in the owning use case, THE Portal frontend SHALL render the Package_Publish_Action in a disabled state accompanied by a message indicating that packaging permission in the owning use case is required.
7. WHEN a user activates the Package_Publish_Action for a vLLM_Model_Record that has a Published_Component, THE Portal frontend SHALL request explicit user confirmation that a new component version will be registered, and SHALL invoke the Packaging_API only after the user confirms.

### Requirement 2: Progress and Result Feedback

**User Story:** As a use case administrator, I want to see the packaging and publish progress and outcome on the model detail page, so that I know when the component is ready for deployment without checking logs or calling APIs.

#### Acceptance Criteria

1. WHEN the Packaging_API responds with success for a Package_Publish_Action invocation, THE Portal frontend SHALL display a confirmation that packaging completed and that component publish was triggered, SHALL record the Published_Component component version present on the record at invocation time (or its absence), and SHALL start polling the model record within 15 seconds of the success response.
2. WHILE polling is active and the polled model record does not contain a Published_Component that is either new since invocation or carries a component version different from the version recorded at invocation time, THE Portal frontend SHALL poll the model record at an interval of no more than 15 seconds and SHALL indicate on the Model_Detail_Page that publish is in progress.
3. WHEN the polled model record contains a Published_Component that was absent at invocation time or whose component version differs from the version recorded at invocation time, THE Portal frontend SHALL stop polling and SHALL display that Published_Component's component name, component version, and supported Target_Architectures on the Model_Detail_Page.
4. WHEN the polled model record contains a Published_Component whose `supported_architectures` list has at least one entry, THE Supported_Architectures_Section SHALL render one badge per Target_Architecture in the Published_Component's `supported_architectures` list, replacing the placeholder text.
5. IF 5 minutes elapse from the start of polling without the record containing a Published_Component that satisfies the completion condition of criterion 3 or a recorded publish failure, THEN THE Portal frontend SHALL stop polling and SHALL display a message stating that publish is still pending and that the page can be refreshed to check again.
6. WHEN a user navigates away from the Model_Detail_Page, THE Portal frontend SHALL stop active polling for that record within one polling interval (15 seconds) and SHALL issue no further poll requests for that record after stopping.
7. IF an individual poll request fails, THEN THE Portal frontend SHALL continue polling at the next scheduled interval without displaying a publish failure and without resetting or extending the 5-minute polling timeout.
8. IF the polled model record contains a Published_Component that satisfies the completion condition of criterion 3 but whose `supported_architectures` list is empty, THEN THE Supported_Architectures_Section SHALL retain the placeholder text and THE Portal frontend SHALL still display the Published_Component's component name and component version.

### Requirement 3: Failure Handling and Retry

**User Story:** As a use case administrator, I want packaging or publish failures reported on the model detail page with a way to retry, so that I can recover from transient errors without leaving the web UI.

#### Acceptance Criteria

1. IF the Packaging_API returns an error response for a Package_Publish_Action invocation, THEN THE Portal frontend SHALL display the error message and failing step contained in the error response on the Model_Detail_Page and SHALL re-enable the Package_Publish_Action for retry.
2. IF the polled model record contains a Packaged_Component_Entry whose status field indicates failure, or contains a recorded publish failure, THEN THE Portal frontend SHALL stop polling, SHALL display the failing step and the failure message recorded on the record on the Model_Detail_Page, and SHALL re-enable the Package_Publish_Action for retry.
3. WHERE the record has at least one Packaged_Component_Entry whose status field indicates success and no Published_Component, THE Portal frontend SHALL offer a publish-only retry action that invokes the Publish_API for the record without re-running packaging.
4. WHEN a user activates the publish-only retry action, THE Portal frontend SHALL invoke the Publish_API for the record's training identifier and SHALL resume the publish progress behavior of Requirement 2, including polling the model record at an interval of no more than 15 seconds, applying the 5-minute polling timeout, and displaying the Published_Component's component name, component version, and supported Target_Architectures when the record contains a Published_Component.
5. IF the Publish_API returns an error response for a publish-only retry invocation, THEN THE Portal frontend SHALL display the error message contained in the error response on the Model_Detail_Page and SHALL re-enable the publish-only retry action.
6. IF a Packaging_API or Publish_API invocation initiated from the Model_Detail_Page receives no response within 30 seconds or fails due to a network error, THEN THE Portal frontend SHALL display an error message indicating the request did not complete and SHALL re-enable the initiating action for retry.
7. WHILE a Publish_API request initiated from the publish-only retry action is in flight, THE Portal frontend SHALL render the publish-only retry action in a loading state and SHALL reject additional activations of the publish-only retry action for the same record.
8. WHEN a user activates the Package_Publish_Action or the publish-only retry action, THE Portal frontend SHALL remove any failure information displayed from a previous packaging or publish attempt for that record before displaying new progress or result feedback.

### Requirement 4: Packaged and Published State Display

**User Story:** As a use case administrator, I want the model detail page to reflect the current packaged and published state of my vLLM model, so that returning to the page shows accurate status without re-running any action.

#### Acceptance Criteria

1. WHEN the Model_Detail_Page loads a vLLM_Model_Record, THE Portal frontend SHALL derive the displayed packaging and publish state solely from the record's Packaged_Component_Entry list and Published_Component map as returned by the model read operation, SHALL display the packaged state section only if the record contains at least one Packaged_Component_Entry, and SHALL display the published state section only if the record contains a Published_Component.
2. WHERE the record has at least one Packaged_Component_Entry with a success status and no Published_Component, THE Model_Detail_Page SHALL display the packaged state including each Packaged_Component_Entry's target and status, alongside the publish-only retry action of Requirement 3.
3. WHERE the record has a Published_Component, THE Model_Detail_Page SHALL display the component name, component version, publish timestamp, and component ARNs from the Published_Component map, and THE Supported_Architectures_Section SHALL render one badge per Target_Architecture in the Published_Component's `supported_architectures` list in place of the placeholder text.
4. WHEN a re-publish of a vLLM_Model_Record completes with a new Published_Component version, THE Model_Detail_Page SHALL display the new component version, publish timestamp, and component ARNs in place of the previous values within one polling interval of no more than 15 seconds while polling is active, or upon the next load of the Model_Detail_Page otherwise.
5. WHERE the vLLM_Model_Record has no Packaged_Component_Entry and no Published_Component, THE Model_Detail_Page SHALL display no packaged or published state sections and THE Supported_Architectures_Section SHALL render the existing placeholder text.
6. WHERE the record loaded by the Model_Detail_Page contains a Packaged_Component_Entry with a failed status and no Published_Component, THE Model_Detail_Page SHALL display the recorded failure information from that Packaged_Component_Entry and SHALL present the retry behavior of Requirement 3 without requiring an active polling session.

### Requirement 5: Backward Compatibility

**User Story:** As an existing portal user, I want model detail behavior for all non-vLLM models to remain unchanged, so that adding this feature carries no regression risk.

#### Acceptance Criteria

1. WHEN the Model_Detail_Page displays a Vision_Model_Record, THE Portal frontend SHALL load the CompilationTab through the existing trained and imported source paths and SHALL display no Package_Publish_Action, no publish-only retry action, and no vLLM packaging or publish state section for that record.
2. WHEN the Package_Publish_Action or publish-only retry action is invoked for a vLLM_Model_Record, THE Portal frontend SHALL call only the existing Packaging_API and Publish_API operations and SHALL send only the request fields defined by those operations' existing request contracts, adding no new fields and requiring no new or modified backend operations.
3. WHEN a vLLM_Model_Record is packaged and published through the Package_Publish_Action, THE resulting Packaged_Component_Entry and Published_Component data SHALL contain the same set of fields with the same value types as the data produced by invoking the Packaging_API with `auto_triggered` set to true directly, differing only in the values of timestamps, component versions, and component ARNs.
4. WHEN the Model_Detail_Page displays a model record that is not a vLLM_Model_Record, THE Portal frontend SHALL display no Package_Publish_Action and no publish-only retry action and SHALL initiate no Published_Component polling for that record.
5. WHEN a CompilationTab package or publish control is activated for a Vision_Model_Record, THE Portal frontend SHALL invoke the same backend operations with the same request contracts that the CompilationTab used before this feature.
