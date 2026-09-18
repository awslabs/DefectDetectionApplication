/**
 * Workflow Tuning section access predicate (quality-prompt-tuning,
 * Requirement 1.1).
 *
 * Single source of truth for "may this role see/use the Workflow Tuning
 * section?", used by the sidebar navigation builder
 * (`components/Layout.tsx`) and the `/workflow-tuning**` route guards
 * (`App.tsx`). Mirrors the `BUILDS_ACCESS_ROLES` / `canAccessBuilds`
 * (`utils/buildsAccess.ts`) and `SYNTHETIC_ACCESS_ROLES` /
 * `canAccessSyntheticData` (`utils/syntheticAccess.ts`) pattern so the
 * gating is directly unit- and property-testable.
 *
 * Hiding the UI does not replace server-side checks: every
 * `/workflow-tuning/anomaly/**` route independently resolves the workflow
 * and calls `authorize_workflow_access` first, auditing denials
 * (Requirements 9.1, 9.2, defense in depth).
 */

import type { UserRole } from '../types';

/**
 * Roles that may edit workflows, and therefore see the Workflow Tuning
 * section — DataScientist, UseCaseAdmin, PortalAdmin (Requirement 1.1).
 *
 * The same three roles as `WORKFLOW_EDIT_ROLES` in
 * `pages/workflows/WorkflowToolbar.tsx`; restated here (rather than
 * imported) so the navigation builder does not pull the workflow designer's
 * module graph into the layout, exactly as the builds and synthetic
 * predicates restate their own role sets.
 */
export const WORKFLOW_TUNING_ACCESS_ROLES: readonly UserRole[] = [
  'DataScientist',
  'UseCaseAdmin',
  'PortalAdmin',
];

/**
 * True when the role may see/use the Workflow Tuning section
 * (Requirement 1.1). A missing role (role-less/loading state) has no
 * access.
 */
export function canAccessWorkflowTuning(
  role: UserRole | undefined | null
): boolean {
  return (
    role !== undefined &&
    role !== null &&
    WORKFLOW_TUNING_ACCESS_ROLES.includes(role)
  );
}
