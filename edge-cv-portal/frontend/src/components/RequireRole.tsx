/**
 * Role-based route guard (build-fleet-rbac-visibility bugfix, design Part B).
 *
 * Renders the guarded page only when the signed-in user's JWT-carried role
 * (`useAuth().user?.role`, the existing frontend gating pattern) is in the
 * allowed set. Otherwise it redirects to `/dashboard` — the authenticated
 * index target — so roles without access never render a page whose only
 * content is a 403 error banner (Requirement 2.5).
 *
 * This is UI gating only. Server-side RBAC remains the ultimate authority:
 * direct API calls by unauthorized clients still receive the standard 403
 * envelope and audit denial (Requirement 2.7, defense in depth).
 */

import { Navigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import type { UserRole } from '../types';

interface RequireRoleProps {
  /** Roles permitted to render `children` (e.g. `BUILDS_ACCESS_ROLES`). */
  roles: readonly UserRole[];
  children: JSX.Element;
}

export default function RequireRole({ roles, children }: RequireRoleProps) {
  const { user } = useAuth();

  // A missing role (role-less or still-resolving user) fails the predicate:
  // redirect rather than render (Requirements 2.5, 2.6).
  if (!user?.role || !roles.includes(user.role)) {
    return <Navigate to="/dashboard" replace />;
  }

  return children;
}
