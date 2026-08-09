/**
 * Unit tests for the builds-access predicate
 * (build-fleet-rbac-visibility bugfix, design Part B).
 *
 * Example-based coverage of the exact role sets: builds access is granted to
 * DataScientist, UseCaseAdmin, and PortalAdmin per the merged
 * role-permission matrix, and denied to Viewer, Operator, and the
 * role-less/loading states (`undefined`, `null`).
 *
 * Validates: Requirements 2.5, 2.6
 */

import { describe, expect, it } from 'vitest';
import { BUILDS_ACCESS_ROLES, canAccessBuilds } from './buildsAccess';
import type { UserRole } from '../types';

const ALL_ROLES: readonly UserRole[] = [
  'PortalAdmin',
  'UseCaseAdmin',
  'DataScientist',
  'Operator',
  'Viewer',
];

describe('canAccessBuilds', () => {
  it.each<UserRole>(['DataScientist', 'UseCaseAdmin', 'PortalAdmin'])(
    'grants builds access to %s',
    (role) => {
      expect(canAccessBuilds(role)).toBe(true);
    }
  );

  it.each<UserRole>(['Viewer', 'Operator'])(
    'denies builds access to %s',
    (role) => {
      expect(canAccessBuilds(role)).toBe(false);
    }
  );

  it('denies builds access for a missing role (undefined / null)', () => {
    expect(canAccessBuilds(undefined)).toBe(false);
    expect(canAccessBuilds(null)).toBe(false);
  });

  it('grants access to exactly the three builds-capable roles', () => {
    const granted = ALL_ROLES.filter((role) => canAccessBuilds(role));
    expect(granted).toEqual(['PortalAdmin', 'UseCaseAdmin', 'DataScientist']);
  });
});

describe('BUILDS_ACCESS_ROLES', () => {
  it('lists the Build_Operator capability roles from the merged matrix', () => {
    expect([...BUILDS_ACCESS_ROLES]).toEqual([
      'DataScientist',
      'UseCaseAdmin',
      'PortalAdmin',
    ]);
  });

  it('agrees with the predicate for every role in the domain', () => {
    for (const role of ALL_ROLES) {
      expect(canAccessBuilds(role)).toBe(BUILDS_ACCESS_ROLES.includes(role));
    }
  });
});
