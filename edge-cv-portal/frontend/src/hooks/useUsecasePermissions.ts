/**
 * Use-case permissions for the bucket configuration UI (spec:
 * portal-usecase-bucket-config, design §10.1).
 *
 * Loads `GET /users/me/usecases` once per signed-in user through react-query
 * and answers two questions the bucket UI needs:
 *
 * - `canUpdateUsecase(usecaseId)`: may the caller change the use case's
 *   bucket configuration (the "Allow for this use case" action)? True for a
 *   PortalAdmin (`is_super_user`) or when the caller holds UseCaseAdmin /
 *   PortalAdmin on that use case.
 * - `isPortalAdmin`: `is_super_user` — gates "Create new bucket" without a
 *   use case.
 *
 * The JWT `custom:role` claim (`useAuth().user.role`) is descriptive only
 * under registry enforcement, while `/users/me/usecases` resolves through the
 * RBAC manager in both modes, so the hook reads the server's answer. The
 * server stays authoritative either way: a stale answer at worst hides the
 * action or renders its 403 as "A UseCaseAdmin can allow buckets for this use
 * case".
 */
import { useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { apiService } from '../services/api';
import { useAuth } from '../contexts/AuthContext';
import type { UserRole } from '../types';

/** Roles that carry UpdateUsecases on a use case. */
export const USECASE_UPDATE_ROLES: readonly UserRole[] = ['UseCaseAdmin', 'PortalAdmin'];

export interface UsecasePermissions {
  canUpdateUsecase: (usecaseId: string | null | undefined) => boolean;
  isPortalAdmin: boolean;
  isLoading: boolean;
}

export function useUsecasePermissions(): UsecasePermissions {
  const { user } = useAuth();
  const { data, isLoading } = useQuery({
    queryKey: ['my-usecases', user?.user_id],
    queryFn: () => apiService.getMyUsecases(),
    enabled: !!user,
  });

  const isPortalAdmin = data?.is_super_user === true;

  const canUpdateUsecase = useCallback(
    (usecaseId: string | null | undefined): boolean => {
      if (!data) return false;
      if (data.is_super_user) return true;
      if (!usecaseId) return false;
      const entry = data.usecases?.find((uc) => uc.usecase_id === usecaseId);
      const role = entry?.user_role;
      return !!role && USECASE_UPDATE_ROLES.includes(role);
    },
    [data]
  );

  return { canUpdateUsecase, isPortalAdmin, isLoading };
}
