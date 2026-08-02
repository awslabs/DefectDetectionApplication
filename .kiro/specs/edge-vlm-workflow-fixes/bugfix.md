# Bugfix Requirements Document

## Introduction

This bugfix addresses three related defects in the edge-device experience for
deploying and managing workflows (LocalServer backend + the on-device
frontend served from `src/frontend`).

1. **VLM models leak into legacy-workflow model selection.** When an operator
   assigns a model to a traditional (legacy) workflow on the edge device, the
   selectable model list includes vLLM / vision-language models (feature type
   `VllmModel`). Legacy workflows cannot run these models, so the operator can
   pick a VLM and produce a broken workflow. The list must exclude
   `VllmModel`-backed entries.

2. **Deployed workflows render the UUID instead of the name.** A deployed
   workflow that carries a human-readable name still renders its opaque
   workflow UUID as its primary identity on the deployed-workflow details
   view. The name should be shown, with the UUID kept only as a fallback for
   older packages that never emitted a name.

3. **Registration details omit the workflow name.** The registration details
   view surfaces the workflow UUID and the registration ID but never the
   human-readable workflow name, leaving the operator with identifiers that
   carry no meaning. The name should be surfaced alongside the identifiers.

The human-readable name is already available: the LocalServer registrations
API (`registration_to_dict` in `src/backend/workflow_engine/api.py`) reads
`workflowName` from the deployed `manifest.json` and returns it as the `name`
field, and the deployed-workflows list surface
(`ListDeployedWorkflows.tsx`) already renders `name` with a UUID fallback. The
details view and the legacy-workflow model list have not been updated to use
it.

Impact: operators building or inspecting workflows on the edge device see
meaningless UUIDs and can misconfigure a legacy workflow with an incompatible
VLM. On-hardware confirmation is planned on JP6 (AGX Orin) and JP5 (Xavier)
via the team's AWS IoT Secure Tunneling device-log handoff procedure; the
conditions below are written so that both the defect and the fix are
observable from the on-device UI / registrations API without device access
during development.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the edge device presents the deployed-model list for assigning a
model to a legacy workflow AND at least one deployed model is vLLM-backed
(feature type `VllmModel`) THEN the system includes the `VllmModel` entries as
selectable options, allowing a VLM to be assigned to a legacy workflow.

1.2 WHEN the deployed-model list is filtered for legacy-workflow assignment
THEN the system applies a filter that always evaluates true (it never actually
excludes any type), so no model type is ever removed from the list.

1.3 WHEN a deployed workflow that has a human-readable name is opened on the
deployed-workflow details view THEN the system renders the workflow UUID as
the page's primary identity instead of the name.

1.4 WHEN the registration details section of a deployed workflow is displayed
THEN the system shows only the workflow UUID and the registration ID and does
not display the human-readable workflow name.

### Expected Behavior (Correct)

2.1 WHEN the edge device presents the deployed-model list for assigning a
model to a legacy workflow AND at least one deployed model is vLLM-backed
(feature type `VllmModel`) THEN the system SHALL exclude all `VllmModel`
entries from the selectable list so a VLM cannot be assigned to a legacy
workflow.

2.2 WHEN the deployed-model list is filtered for legacy-workflow assignment
THEN the system SHALL retain exactly the non-VLM model types (`LFVModel` and
`TritonModel`) and drop every `VllmModel` entry.

2.3 WHEN a deployed workflow that has a human-readable name is opened on the
deployed-workflow details view THEN the system SHALL render the workflow name
as the page's primary identity, using the workflow UUID only when no name is
available.

2.4 WHEN the registration details section of a deployed workflow is displayed
AND the workflow has a human-readable name THEN the system SHALL display that
name alongside the existing identifiers (workflow UUID and registration ID).

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the deployed-model list for legacy-workflow assignment contains
`LFVModel` or `TritonModel` entries THEN the system SHALL CONTINUE TO present
those non-VLM models as selectable options.

3.2 WHEN a `VllmModel` entry is returned by any surface that legitimately
lists all feature configurations (for example the general feature-configuration
listing and model-status reporting) THEN the system SHALL CONTINUE TO include
the `VllmModel` entry unchanged; only the legacy-workflow assignment list
excludes it.

3.3 WHEN a deployed workflow has no human-readable name (a package built before
the packager emitted `workflowName`) THEN the system SHALL CONTINUE TO display
the workflow UUID on both the details view and in the registration details.

3.4 WHEN the deployed-workflows list surface renders a workflow THEN the system
SHALL CONTINUE TO show the human-readable name with a UUID fallback exactly as
it does today.

3.5 WHEN the registration details view displays the registration ID, version,
architecture, status, and registered timestamp THEN the system SHALL CONTINUE
TO display those fields unchanged.

3.6 WHEN a legacy workflow already has a non-VLM model assigned THEN the system
SHALL CONTINUE TO load, display, and run that workflow's model unchanged.
