/**
 * Synthetic data generation access predicate
 * (synthetic-defect-data-generation, Requirement 9.3).
 *
 * Single source of truth for "may this role see/use the synthetic data
 * generation workspace?", used by the sidebar navigation builder
 * (`components/Layout.tsx`) and the `/synthetic` route guards (`App.tsx`).
 * Mirrors the `BUILDS_ACCESS_ROLES` / `canAccessBuilds` pattern in
 * `utils/buildsAccess.ts` so the gating is directly property-testable.
 *
 * Hiding the UI does not replace server-side checks: every synthetic API
 * route independently enforces Data_Scientist_Access and audits denials
 * (Requirements 9.1, 9.2, defense in depth).
 */

import type { UserRole } from '../types';

/**
 * Roles holding Data_Scientist_Access per the portal role hierarchy —
 * DataScientist plus the roles that satisfy it (UseCaseAdmin, PortalAdmin).
 */
export const SYNTHETIC_ACCESS_ROLES: readonly UserRole[] = [
  'DataScientist',
  'UseCaseAdmin',
  'PortalAdmin',
];

/**
 * True when the role may see/use the synthetic data workspace (Req 9.3).
 * A missing role (role-less/loading state) has no access.
 */
export function canAccessSyntheticData(
  role: UserRole | undefined | null
): boolean {
  return (
    role !== undefined && role !== null && SYNTHETIC_ACCESS_ROLES.includes(role)
  );
}
