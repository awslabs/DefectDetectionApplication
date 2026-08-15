/**
 * Data_Labeler route guard (dda-data-labeling, design §8 "RBAC").
 *
 * Generalizes the `RequireRole` pattern: instead of gating a single route,
 * this wraps the authenticated layout and redirects a user whose resolved
 * role is exactly `DataLabeler` away from every route except the labeler
 * workspace (`/labeler`) to `/labeler`, without rendering the requested
 * page (Requirement 2.7).
 *
 * Routes intentionally outside this guard:
 * - `/login` is not nested under the authenticated layout, so it is never
 *   intercepted.
 * - Account settings (the change-password action) is a modal inside
 *   `Layout`, not a route, so it stays reachable from `/labeler`
 *   (Requirement 2.2).
 *
 * Multi-role users resolve to a role other than `DataLabeler`, so the
 * guard does not apply to them and their navigation is unchanged
 * (Requirement 2.8). This is UI gating only — server-side RBAC remains
 * the ultimate authority (Requirement 2.3).
 */

import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';

export default function DataLabelerRedirect({
  children,
}: {
  children: JSX.Element;
}) {
  const { user } = useAuth();
  const location = useLocation();

  const isDataLabelerOnly = user?.role === 'DataLabeler';
  const isLabelerRoute =
    location.pathname === '/labeler' ||
    location.pathname.startsWith('/labeler/');

  if (isDataLabelerOnly && !isLabelerRoute) {
    return <Navigate to="/labeler" replace />;
  }

  return children;
}
