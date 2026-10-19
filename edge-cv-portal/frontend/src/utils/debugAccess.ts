/**
 * Workflow debug mode access predicates (workflow-debug-mode,
 * Requirements 3.1, 6.3).
 *
 * Single source of truth for "may this role start or stop a debug session?"
 * and "may this role read the debug log?", used by the sidebar navigation
 * builder (`components/Layout.tsx`), the `/debug/**` route guards
 * (`App.tsx`), the sessions page and the Device detail "Debug" tab. Mirrors
 * the `SYNTHETIC_ACCESS_ROLES` / `canAccessSyntheticData` shape of
 * `utils/syntheticAccess.ts` so the gating is directly unit-testable.
 *
 * Hiding the UI does not replace server-side checks: every debug route
 * independently calls `has_permission` (`MANAGE_DEVICES` to start and stop,
 * `VIEW_DEVICE_LOGS` to read) and audits denials (design §7.2).
 */

import type { UserRole } from '../types';

/**
 * Roles holding `MANAGE_DEVICES` per the portal role matrix: Operator,
 * UseCaseAdmin and PortalAdmin (design §7.2, D-E).
 */
export const DEBUG_MANAGE_ROLES: readonly UserRole[] = [
  'Operator',
  'UseCaseAdmin',
  'PortalAdmin',
];

/**
 * Roles holding `VIEW_DEVICE_LOGS`: every role except DataLabeler, whose
 * navigation is the labeler workspace only (dda-data-labeling Req 2.2).
 */
export const DEBUG_VIEW_ROLES: readonly UserRole[] = [
  'PortalAdmin',
  'UseCaseAdmin',
  'DataScientist',
  'Operator',
  'Viewer',
];

/**
 * True when the role may start and stop debug sessions. A missing role
 * (role-less/loading state) may not.
 */
export function canManageDebug(role: UserRole | undefined | null): boolean {
  return role !== undefined && role !== null && DEBUG_MANAGE_ROLES.includes(role);
}

/**
 * True when the role may read the debug log (the sessions page, the viewer
 * and the "Debug log" navigation entry). A missing role may not.
 */
export function canViewDebugLog(role: UserRole | undefined | null): boolean {
  return role !== undefined && role !== null && DEBUG_VIEW_ROLES.includes(role);
}
