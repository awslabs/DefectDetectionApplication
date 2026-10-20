/**
 * Workflow telemetry dashboards access predicates
 * (workflow-telemetry-dashboards design §11.1, §11.4; tasks.md 5.1).
 *
 * Single source of truth for "may this role read dashboards?", "may it
 * create and change them?" and "may it switch telemetry on and tune it?",
 * used by the sidebar navigation builder (`components/Layout.tsx`), the
 * `/workflow-dashboards/**` and `/telemetry-settings/**` route guards
 * (`App.tsx`) and the pages. Mirrors the `debugAccess.ts` /
 * `syntheticAccess.ts` shape so the gating is directly unit-testable.
 *
 * The matrix is read from `telemetryPermissionsMatrix.json`, the one table
 * of design §11.1 that the backend test `test_telemetry_permissions.py`
 * asserts against `shared_utils.RBACManager` too, so the two sides cannot
 * drift apart silently.
 *
 * Hiding the UI does not replace server-side checks: every telemetry route
 * independently calls `has_permission` (`dashboards:read`,
 * `dashboards:manage`, `telemetry:manage`) and audits denials (design
 * §11.2). Under registry enforcement the effective role comes from the
 * registry row, so a hidden action at worst hides what the server would
 * refuse anyway.
 */

import type { UserRole } from '../types';
import matrix from './telemetryPermissionsMatrix.json';

export type TelemetryPermission = 'dashboards:read' | 'dashboards:manage' | 'telemetry:manage';

/** The §11.1 table: role → permission → granted. */
export const TELEMETRY_PERMISSION_MATRIX: Readonly<Record<UserRole, Readonly<Record<TelemetryPermission, boolean>>>> =
  matrix as Record<UserRole, Record<TelemetryPermission, boolean>>;

/** Every role of the matrix, in the order the backend lists them. */
export const TELEMETRY_MATRIX_ROLES: readonly UserRole[] = Object.keys(TELEMETRY_PERMISSION_MATRIX) as UserRole[];

function rolesHolding(permission: TelemetryPermission): readonly UserRole[] {
  return TELEMETRY_MATRIX_ROLES.filter((role) => TELEMETRY_PERMISSION_MATRIX[role][permission]);
}

/** Roles holding `dashboards:read`: every role except DataLabeler. */
export const DASHBOARDS_READ_ROLES: readonly UserRole[] = rolesHolding('dashboards:read');

/** Roles holding `dashboards:manage`: Operator, DataScientist, UseCaseAdmin, PortalAdmin. */
export const DASHBOARDS_MANAGE_ROLES: readonly UserRole[] = rolesHolding('dashboards:manage');

/** Roles holding `telemetry:manage`: UseCaseAdmin and PortalAdmin. */
export const TELEMETRY_MANAGE_ROLES: readonly UserRole[] = rolesHolding('telemetry:manage');

/** True when the matrix grants `permission` to `role`; a missing role holds nothing. */
export function hasTelemetryPermission(role: UserRole | undefined | null, permission: TelemetryPermission): boolean {
  if (role === undefined || role === null) return false;
  const row = TELEMETRY_PERMISSION_MATRIX[role];
  return row !== undefined && row[permission] === true;
}

/** True when the role may list and open dashboards and read their tiles. */
export function canReadDashboards(role: UserRole | undefined | null): boolean {
  return hasTelemetryPermission(role, 'dashboards:read');
}

/** True when the role may create, change and delete dashboards. */
export function canManageDashboards(role: UserRole | undefined | null): boolean {
  return hasTelemetryPermission(role, 'dashboards:manage');
}

/** True when the role may switch telemetry on for a use case and tune it. */
export function canManageTelemetry(role: UserRole | undefined | null): boolean {
  return hasTelemetryPermission(role, 'telemetry:manage');
}
