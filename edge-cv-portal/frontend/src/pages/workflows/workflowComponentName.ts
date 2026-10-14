/**
 * Friendly display names for packaged Workflow_Components
 * (dda.workflow.{workflowId}).
 *
 * A packaged workflow's Greengrass component name is
 * `dda.workflow.{workflowId}` — the workflow UUID, which is meaningless
 * to users on the components list and the deployment picker. The
 * packaging pipeline records only the workflow id on the component (tag
 * / recipe configuration), not the name, so the human-readable workflow
 * name is resolved client-side from the Workflow_Store listing
 * (`listWorkflows`) keyed by workflow id.
 */

/** Keep in sync with workflow_packaging.py WORKFLOW_COMPONENT_PREFIX. */
export const WORKFLOW_COMPONENT_PREFIX = 'dda.workflow.';

/** True when a component is a packaged Workflow_Component. */
export function isWorkflowComponent(
  componentName: string | null | undefined
): boolean {
  return (
    typeof componentName === 'string' &&
    componentName.startsWith(WORKFLOW_COMPONENT_PREFIX)
  );
}

/** The workflow id encoded in a `dda.workflow.{id}` component name, or null. */
export function workflowIdFromComponent(
  componentName: string | null | undefined
): string | null {
  if (!isWorkflowComponent(componentName)) return null;
  return (componentName as string).slice(WORKFLOW_COMPONENT_PREFIX.length);
}

/**
 * The friendly workflow name for a `dda.workflow.{id}` component given a
 * `{workflowId: name}` map, or null when the component is not a workflow
 * component or its name is not resolvable (caller falls back to the raw
 * component name).
 */
export function workflowComponentName(
  componentName: string | null | undefined,
  namesById: Record<string, string>
): string | null {
  const id = workflowIdFromComponent(componentName);
  if (id === null) return null;
  const name = namesById[id];
  return name && name.trim() !== '' ? name : null;
}
