# Bugfix Requirements Document

## Introduction

The trigger-activation-runtime feature (Requirement 10.2) requires that any Greengrass deployment whose component set carries both a subscribing workflow component (a workflow version whose item records non-empty `subscribed_topics`) and a LocalServer component merges the per-workflow `dda:workflow-subscribe:<workflowId>` accessControl policy (`aws.greengrass#SubscribeToIoTCore`, resources = the recorded topic filters) into the LocalServer entry's `configurationUpdate.merge`.

That merge was wired into the generic `create_deployment` path in `edge-cv-portal/backend/functions/deployments.py` — but the portal's WORKFLOW deployment path, `create_workflow_deployment` (used when the request body carries `component_type: workflow` / `workflow_id`, which is the path the portal's workflow deploy page uses), builds its own components map (merging the target's existing component set, which includes LocalServer) and submits the Greengrass deployment directly WITHOUT applying the subscribe accessControl merge.

Result: deploying a subscribing workflow through the portal ships no subscribe grant. On-device, the Greengrass IPC broker denies the LocalServer's `SubscribeToIoTCore` call with `UnauthorizedError` and the workflow's mqtt_subscribe trigger never fires.

Confirmed live on ryan-orin-nano (2026-08-05): workflow `bedrock_test` v5 (mqtt_subscribe node, greengrass path, topic `dda/bedrock-test-trigger`) was packaged and portal-deployed; device logs show `Greengrass IPC denied SubscribeToIoTCore for topic 'dda/bedrock-test-trigger'` and the trigger never subscribes.

A second, related facet: `collect_workflow_subscribed_topics` resolves each workflow component's version item by parsing the MAJOR out of the entry's `componentVersion` (`workflow_guards.get_version_item(workflow_id, major)`). Re-packaging bumps the component major past the workflow version (`next_component_version` returns `max(workflow_version, highest_major + 1)`), and the version item never records the resolved component version — so a bumped-major workflow component entry (chosen by the caller on the generic path, or carried over from an existing deployment) makes the version-item lookup silently resolve nothing and the topics go uncollected. The workflow deployment path knows the exact workflow version being deployed (it already loaded that version item), so its topic resolution must come from that authoritative version, not from a blind component-major parse.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN `create_workflow_deployment` submits a deployment for a workflow version whose item records non-empty `subscribed_topics`, targeting a device whose merged component set contains a LocalServer component, THEN the system submits a Greengrass deployment whose LocalServer entry lacks the `dda:workflow-subscribe:<workflowId>` accessControl policy, and the on-device subscribe is denied with `UnauthorizedError`

1.2 WHEN `create_workflow_deployment` submits a deployment for a subscribing workflow version and the merged component set contains NO LocalServer component, THEN the system submits the deployment silently, with no warning in the response that the subscribe authorization had nowhere to attach

1.3 WHEN a deployment component set carries a workflow component whose `componentVersion` major was bumped past the workflow version by re-packaging, THEN the system's `collect_workflow_subscribed_topics` looks up the version item by the component major, resolves nothing, and silently collects no topics for that workflow

### Expected Behavior (Correct)

2.1 WHEN `create_workflow_deployment` submits a deployment for a workflow version whose item records non-empty `subscribed_topics`, targeting a device whose merged component set contains a LocalServer component, THEN the system SHALL merge the `dda:workflow-subscribe:<workflowId>` policy (`aws.greengrass#SubscribeToIoTCore`, resources = exactly the recorded topic filters) into the LocalServer entry's `configurationUpdate.merge` after the components map is final (after merging the target's existing component set and adding the workflow component) and before `greengrass_client.create_deployment` is called

2.2 WHEN `create_workflow_deployment` submits a deployment for a subscribing workflow version and the merged component set contains NO LocalServer component, THEN the system SHALL include the actionable warning(s) in its 201 response under an additive `warnings` field (present only when non-empty, matching `create_deployment`'s pattern)

2.3 WHEN the subscribe accessControl merge runs on the workflow deployment path, THEN the system SHALL resolve the deployed workflow's subscribed topics from the authoritative workflow version being deployed (the version item `create_workflow_deployment` already loaded), not from a blind parse of the component entry's version major

### Unchanged Behavior (Regression Prevention)

3.1 WHEN `create_workflow_deployment` deploys a workflow version whose item records no `subscribed_topics` (or an empty list), THEN the system SHALL CONTINUE TO submit a byte-identical deployment (no accessControl merge, no new keys anywhere in the components map) and return a response without a `warnings` field

3.2 WHEN the generic `create_deployment` path handles any component set, THEN the system SHALL CONTINUE TO apply the subscribe accessControl merge and surface warnings exactly as it does today (all existing deployment tests pass unchanged)

3.3 WHEN `create_workflow_deployment` revises an existing deployment, THEN the system SHALL CONTINUE TO preserve the target's existing components (LocalServer and others), replace the older workflow component version, reuse the existing deployment name, and record the association/audit entries as it does today

3.4 WHEN `create_workflow_deployment` runs its pre-submit gates (validation guard, packaged-component check, LocalServer compatibility, plugin gates, vLLM gate, camera binding validation and delivery), THEN the system SHALL CONTINUE TO enforce them with unchanged error envelopes and ordering

3.5 WHEN `apply_subscribe_access_control` is called with its existing single argument on the generic path, THEN the system SHALL CONTINUE TO produce the same merges and warnings as today, including deep-merging into an existing LocalServer `configurationUpdate.merge` non-destructively and never clobbering an unparseable caller-supplied merge
