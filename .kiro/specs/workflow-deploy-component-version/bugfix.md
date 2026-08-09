# Bugfix Requirements Document

## Introduction

The portal's workflow deployment path, `create_workflow_deployment` in `edge-cv-portal/backend/functions/deployments.py`, sets the deployed workflow component entry to `workflow_component_version(workflow_version)` = `{workflow_version}.0.0` — it never resolves the ACTUAL registered Greengrass component version for the selected workflow version. But re-packaging a workflow version deliberately bumps the component MAJOR: `workflow_packaging.py::next_component_version` returns `max(workflow_version, highest_major + 1)` because Greengrass component versions are immutable and patch/minor bumps can leave stale artifacts on-device. Consequence: deploying a re-packaged workflow version through the portal pins the OLD component version. Greengrass sees no component change and delivers nothing — the deployment reports COMPLETED while the fixed component never reaches the device.

Confirmed live on ryan-orin-nano (2026-08-06): workflow `modbus_test` (`e830f55d-5744-4edf-be43-1a33fbd4605d`) v1 was re-packaged to component `2.0.0` (fixed recipe) after `1.0.0` shipped a broken recipe; portal deploy `72c2f784` (revision 14) pinned `dda.workflow.e830f55d...` at componentVersion `1.0.0`; the device reported COMPLETED but installed nothing — artifacts still missing, workflow unregistered.

The authoritative component version IS available to the function: the version item `create_workflow_deployment` already loads (and gates on) records `component_arn`, which ends `:versions:{component_version}` (e.g. `...:versions:2.0.0`). The packaging side does not currently record `component_version` as a discrete field on the version item.

A related facet (documented as facet 1.3 of `.kiro/specs/workflow-deploy-subscribe-merge/bugfix.md` and deferred there): several consumers derive a workflow version by parsing the MAJOR out of a component entry's `componentVersion` and looking up `workflow_guards.get_version_item(workflow_id, major)` — `collect_workflow_subscribed_topics` (subscribe grant merge), the vLLM gate manifest builder, and `_deployed_workflow_binding_keys` (camera-binding shadow prune keys). Once a component entry carries a bumped major (from the corrected deploy path, from the generic path, or carried over from an existing deployment), the major no longer equals a workflow version: the lookup silently resolves nothing (or the wrong item), topics go uncollected (no subscribe grant → on-device `SubscribeToIoTCore` denial), and binding keys are misderived. Live example: `bedrock_test` (`f81a4c66-...`) workflow v5 runs component `6.0.0`; a fresh grant-merge for it would look up version item 6 (nonexistent — `latest_version` is 5) and collect nothing; its grant currently survives only because `apply_subscribe_access_control` preserves pre-existing merge keys on carry-over. An authoritative componentVersion→workflow_version resolution is derivable from the packaging side (e.g. record `component_version` on the version item at package time and/or resolve via version items' `component_arn`s).

Working-tree note: `deployments.py` and `workflow_packaging.py` carry uncommitted verified fixes (subscribe-merge, run-script cleanup). This spec's scope builds on the current working-tree state, not HEAD.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN `create_workflow_deployment` deploys a workflow version that has been re-packaged (its registered component version's major exceeds the workflow version, per `next_component_version`) THEN the system pins the workflow component entry at `{workflow_version}.0.0` — the OLD component version — so Greengrass resolves no component change, delivers no artifacts, and reports the deployment COMPLETED while the device still runs (or lacks) the old component

1.2 WHEN `create_workflow_deployment` records the association record, audit log entry, and 201 response for a re-packaged workflow version THEN the system records `component_version` = `{workflow_version}.0.0` instead of the actually registered component version, misrepresenting what was deployed

1.3 WHEN a deployment component set carries a workflow component entry whose `componentVersion` major was bumped past the workflow version THEN the system's `collect_workflow_subscribed_topics` looks up the version item by the component major, silently resolves nothing (or a wrong item), collects no subscribed topics, and the subscribe accessControl grant is never merged — the on-device `SubscribeToIoTCore` call is denied

1.4 WHEN a deployment component set carries a workflow component entry whose `componentVersion` major was bumped past the workflow version THEN the system's other major-parse consumers misresolve the workflow version: `_deployed_workflow_binding_keys` derives a wrong camera-binding key (so a valid binding key can be pruned from the device shadow) and the vLLM gate manifest builder resolves a wrong or missing version item (so an LLM-bearing workflow can evade the architecture gate)

1.5 WHEN `workflow_packaging.py` registers a re-packaged component THEN the system records only `component_arn` on the version item — no discrete `component_version` field — leaving downstream consumers no direct authoritative componentVersion→workflow_version mapping

### Expected Behavior (Correct)

2.1 WHEN `create_workflow_deployment` deploys a workflow version THEN the system SHALL set the workflow component entry to the ACTUAL registered component version for that workflow version, resolved authoritatively from the version item it already loads (the `component_arn` suffix `:versions:{component_version}`, and/or a recorded `component_version` field), falling back to `{workflow_version}.0.0` ONLY when no such authoritative record exists

2.2 WHEN `create_workflow_deployment` deploys a re-packaged workflow version THEN the system SHALL record the actually deployed component version in the association record, the audit log entry, and the 201 response's `component_version` field

2.3 WHEN a deployment component set carries a workflow component entry whose `componentVersion` major was bumped past the workflow version (whether set by the corrected deploy path, chosen by a caller on the generic path, or carried over from an existing deployment) THEN the system SHALL resolve that entry's subscribed topics from the correct workflow version item, so the `dda:workflow-subscribe:<workflowId>` grant merges for subscribing workflows regardless of re-packaging history

2.4 WHEN a deployment component set carries a workflow component entry whose `componentVersion` major was bumped past the workflow version THEN the system SHALL resolve the correct workflow version for the camera-binding key derivation (`_deployed_workflow_binding_keys`) and the vLLM gate manifest builder, so binding keys are preserved on prune and LLM-bearing workflows remain gated

2.5 WHEN `workflow_packaging.py` registers a workflow component version THEN the system SHALL record the resolved component version on the version item so that a componentVersion→workflow_version resolution is directly available to deployment-path consumers (with `component_arn` parsing available as a fallback for versions packaged before this change)

### Unchanged Behavior (Regression Prevention)

3.1 WHEN `create_workflow_deployment` deploys a workflow version whose registered component version equals `{workflow_version}.0.0` (first package, never re-packaged) THEN the system SHALL CONTINUE TO submit exactly the deployment it submits today — same component entry, same association record, same audit entry, same response body

3.2 WHEN the generic `create_deployment` path handles any component set THEN the system SHALL CONTINUE TO honor the caller's explicit component versions and produce its existing behavior unchanged (all existing deployment tests pass unchanged)

3.3 WHEN `create_workflow_deployment` revises an existing deployment THEN the system SHALL CONTINUE TO preserve the target's existing components, replace the older workflow component entry, reuse the existing deployment name, and record association/audit entries with today's semantics (only the componentVersion value is corrected)

3.4 WHEN `create_workflow_deployment` runs its pre-submit gates (validation guard, packaged-component check, LocalServer compatibility, plugin gates, vLLM gate, camera binding validation and delivery) THEN the system SHALL CONTINUE TO enforce them with unchanged error envelopes and ordering

3.5 WHEN a deployment's component set contains no subscribing workflow THEN the system SHALL CONTINUE TO submit a byte-identical component set (no accessControl merge, no new keys) — modulo only the corrected workflow componentVersion — and return a response without a `warnings` field

3.6 WHEN `apply_subscribe_access_control` merges subscribe grants THEN the system SHALL CONTINUE TO merge non-destructively into any existing LocalServer `configurationUpdate.merge` (preserving pre-existing keys), never clobber an unparseable caller-supplied merge, and surface the same actionable warnings when the LocalServer component is absent

3.7 WHEN a workflow version is packaged for the first time THEN `workflow_packaging.py` SHALL CONTINUE TO assign component version `{workflow_version}.0.0`, and re-packaging SHALL CONTINUE TO bump to the next free major per `next_component_version` — the version-numbering scheme itself is unchanged

3.8 WHEN the uncommitted working-tree fixes in `deployments.py` and `workflow_packaging.py` (subscribe-merge on the workflow deploy path, run-script cleanup) are exercised THEN the system SHALL CONTINUE TO exhibit those verified behaviors — this fix builds on the current working-tree state, not HEAD
