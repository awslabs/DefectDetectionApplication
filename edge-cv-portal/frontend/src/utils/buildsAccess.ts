/**
 * Builds surface access predicate (build-fleet-rbac-visibility bugfix,
 * design Part B).
 *
 * Single source of truth for "may this role see/use the builds surface?",
 * used by the sidebar navigation builder (`components/Layout.tsx`) and the
 * `/builds`, `/builds/:buildJobId` route guards (`App.tsx`). Mirrors the
 * exported-pure-function pattern of `WORKFLOW_EDIT_ROLES` /
 * `canEditWorkflows` in `pages/workflows/WorkflowToolbar.tsx` so the gating
 * is directly unit- and property-testable.
 *
 * Hiding the UI does not replace server-side checks: unauthorized direct API
 * calls still receive the standard 403 envelope and audit denial
 * (Requirement 2.7, defense in depth).
 */

import type { UserRole } from '../types';

/**
 * Roles granted builds:read / builds:submit / builds:cancel per the merged
 * role-permission matrix — the Build_Operator capability
 * (Requirement 2.6; matrix itself unchanged per Requirement 3.4).
 */
export const BUILDS_ACCESS_ROLES: readonly UserRole[] = [
  'DataScientist',
  'UseCaseAdmin',
  'PortalAdmin',
];

/**
 * True when the role may see/use the builds surface (Requirements 2.5, 2.6).
 * A missing role (role-less/loading state) has no builds access.
 */
export function canAccessBuilds(role: UserRole | undefined | null): boolean {
  return role !== undefined && role !== null && BUILDS_ACCESS_ROLES.includes(role);
}
