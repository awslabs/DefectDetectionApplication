/**
 * Node_Designer mutating-surface access predicate (custom-node-source-
 * lifecycle Requirements 9.1, 9.2).
 *
 * Single source of truth for "may this role save source, sync with Git,
 * add architectures, or use the Code_Assistant on the Node_Designer?"
 * Mirrors `utils/buildsAccess.ts`. The backend enforces the same rule
 * through `node-designer:manage` / `node-designer:generate` (UseCaseAdmin
 * within the Use_Case or PortalAdmin); hiding the controls here is defense
 * in depth, never the only check.
 */

import type { UserRole } from '../types';

export const NODE_DESIGNER_MANAGE_ROLES: readonly UserRole[] = ['UseCaseAdmin', 'PortalAdmin'];

/** True when the role may perform Node_Designer mutations (9.1). */
export function canManageNodeDesigner(role: UserRole | undefined | null): boolean {
  return role !== undefined && role !== null && NODE_DESIGNER_MANAGE_ROLES.includes(role);
}
