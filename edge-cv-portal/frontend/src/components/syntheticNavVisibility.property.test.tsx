/**
 * **Feature: synthetic-defect-data-generation, Property 14: Navigation visibility by role (frontend)**
 *
 * *For any* portal role (including the role-less/loading state): the
 * navigation items produced for that role SHALL include the synthetic data
 * generation workspace entry if and only if the role is DataScientist,
 * UseCaseAdmin, or PortalAdmin.
 *
 * **Validates: Requirements 9.3**
 *
 * Exercises the exported pure `buildNavigationItems` function (the same
 * pattern as `buildsSurfaceVisibility.property.test.tsx`) with fast-check,
 * minimum 100 iterations. The oracle is re-derived locally from the
 * Data_Scientist_Access definition (DataScientist, UseCaseAdmin,
 * PortalAdmin) rather than imported from the production predicate, so the
 * test does not depend on `canAccessSyntheticData` for its notion of
 * correctness.
 */
import { describe, expect, it } from 'vitest';
import * as fc from 'fast-check';
import { buildNavigationItems } from './Layout';
import type { UserRole } from '../types';

// ---------------------------------------------------------------- oracle

/** Roles holding Data_Scientist_Access per the requirements glossary. */
function oracleCanAccessSynthetic(role: UserRole | undefined): boolean {
  return (
    role === 'DataScientist' ||
    role === 'UseCaseAdmin' ||
    role === 'PortalAdmin'
  );
}

const ALL_ROLES: readonly UserRole[] = [
  'PortalAdmin',
  'UseCaseAdmin',
  'DataScientist',
  'Operator',
  'Viewer',
];

/** Full domain: every role plus the role-less / still-loading state. */
const roleArb: fc.Arbitrary<UserRole | undefined> = fc.constantFrom(
  ...ALL_ROLES,
  undefined
);

/** True iff the nav item list contains the synthetic workspace entry. */
function navHasSyntheticEntry(role: UserRole | undefined): boolean {
  return buildNavigationItems(role).some(
    (item) =>
      item.type === 'link' &&
      (item.href === '/synthetic' || item.text === 'Synthetic Data')
  );
}

// ----------------------------------------------------------------- tests

describe('Property 14: Navigation visibility by role (frontend)', () => {
  it('includes the synthetic data entry iff the role is DataScientist, UseCaseAdmin, or PortalAdmin', () => {
    fc.assert(
      fc.property(roleArb, (role) => {
        expect(
          navHasSyntheticEntry(role),
          `role=${role}: "Synthetic Data" nav entry visibility`
        ).toBe(oracleCanAccessSynthetic(role));
      }),
      { numRuns: 100 }
    );
  });

  it('never exposes a /synthetic href to roles without Data_Scientist_Access', () => {
    fc.assert(
      fc.property(roleArb, (role) => {
        fc.pre(!oracleCanAccessSynthetic(role));
        const hrefs = buildNavigationItems(role)
          .filter((item) => item.type === 'link')
          .map((item) => (item as { href?: string }).href ?? '');
        expect(
          hrefs.some((href) => href.startsWith('/synthetic')),
          `role=${role}: found a /synthetic href`
        ).toBe(false);
      }),
      { numRuns: 100 }
    );
  });
});
