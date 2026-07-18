# Bugfix Requirements Document

## Introduction

Portal-built workflows deployed to an edge device as Greengrass components are discovered and registered by LocalServer's workflow engine, and the edge HTTP API fully supports listing registrations, viewing registration details with executions, triggering runs, and checking run status. However, the LocalServer web frontend has no page for any of this: its routes only cover the legacy Pipeline_Configuration workflow pages. An operator standing at the device therefore has no way to see cloud-deployed workflow registrations or run them from the UI, even though the backend capability exists and works. This fix adds the missing UI (a LocalServer frontend page plus API client over the existing registrations/executions endpoints) without touching the legacy Pipeline_Configuration pages or the backend.

## Bug Analysis

### Current Behavior (Defect)

When one or more cloud-deployed workflow registrations exist on the device (status `registered` or `invalid`), the LocalServer UI renders no view or control for them.

1.1 WHEN a cloud-deployed workflow registration exists on the device THEN the LocalServer UI displays no page or list showing that registration or its status

1.2 WHEN an operator wants to run a registered cloud-deployed workflow from the device THEN the LocalServer UI provides no control to trigger the run, forcing use of the raw HTTP API

1.3 WHEN executions of a cloud-deployed workflow have been triggered THEN the LocalServer UI displays no execution status or history for that workflow

1.4 WHEN a cloud-deployed workflow registration is invalid THEN the LocalServer UI gives the operator no indication that the registration exists, is invalid, or why it is invalid

### Expected Behavior (Correct)

The LocalServer UI surfaces cloud-deployed workflow registrations and lets the operator run registered workflows and monitor executions, using the existing edge HTTP API (`GET /workflows/registrations`, `GET /workflows/registrations/{id}`, `POST /workflows/registrations/{id}/trigger`, `GET /workflows/executions/{id}`).

2.1 WHEN a cloud-deployed workflow registration exists on the device THEN the LocalServer UI SHALL display it in a list of workflow registrations showing its identity (workflow, version) and status

2.2 WHEN an operator selects a registration with status `registered` THEN the LocalServer UI SHALL provide a control that triggers a run of that workflow via the trigger endpoint

2.3 WHEN executions exist for a cloud-deployed workflow registration THEN the LocalServer UI SHALL display the execution status and history for that registration, including failure details when an execution failed

2.4 WHEN a cloud-deployed workflow registration is invalid THEN the LocalServer UI SHALL display the registration with its invalid status and invalid reason, and SHALL NOT offer a control to trigger a run of it

2.5 WHEN no cloud-deployed workflow registrations exist on the device THEN the LocalServer UI SHALL display an empty state on the registrations page without error

### Unchanged Behavior (Regression Prevention)

The fix is UI-only and additive. The legacy Pipeline_Configuration pages and endpoints, all other LocalServer UI pages, and the backend workflow engine must be untouched.

3.1 WHEN a user navigates the legacy Pipeline_Configuration workflow pages (list, details, edit) THEN the LocalServer UI SHALL CONTINUE TO render them with unchanged behavior, calling the legacy `/workflows` endpoints via the existing API client

3.2 WHEN a user navigates any other existing LocalServer UI page (image sources, models, live results, result history, image capture, application health) THEN the LocalServer UI SHALL CONTINUE TO render and behave as before

3.3 WHEN the edge HTTP API serves the registrations and executions endpoints THEN the backend SHALL CONTINUE TO behave exactly as it does today, with no backend changes made by this fix

3.4 WHEN a trigger is requested for an invalid registration THEN the system SHALL CONTINUE TO reject the run (the existing backend 409 rejection per the workflow-manager spec's rule that invalid registrations can never be run)
