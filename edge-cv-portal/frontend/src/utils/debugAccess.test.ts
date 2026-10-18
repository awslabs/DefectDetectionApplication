/**
 * Unit tests for the workflow debug mode access predicates
 * (workflow-debug-mode task 5.1, Requirements 3.1, 6.3).
 *
 * The two matrices over every `UserRole`: starting and stopping a session
 * (`MANAGE_DEVICES`: Operator, UseCaseAdmin, PortalAdmin) and reading the
 * debug log (`VIEW_DEVICE_LOGS`: every role except DataLabeler), plus the
 * role-less/loading states.
 */

import { describe, expect, it } from 'vitest';
import {
  DEBUG_MANAGE_ROLES,
  DEBUG_VIEW_ROLES,
  canManageDebug,
  canViewDebugLog,
} from './debugAccess';
import type { UserRole } from '../types';

const ALL_ROLES: readonly UserRole[] = [
  'PortalAdmin',
  'UseCaseAdmin',
  'DataScientist',
  'Operator',
  'Viewer',
  'DataLabeler',
];

describe('canManageDebug (MANAGE_DEVICES)', () => {
  it.each<UserRole>(['Operator', 'UseCaseAdmin', 'PortalAdmin'])(
    'lets %s start and stop sessions',
    (role) => {
      expect(canManageDebug(role)).toBe(true);
    }
  );

  it.each<UserRole>(['Viewer', 'DataScientist', 'DataLabeler'])(
    'does not let %s start or stop sessions',
    (role) => {
      expect(canManageDebug(role)).toBe(false);
    }
  );

  it('denies a missing role (undefined / null)', () => {
    expect(canManageDebug(undefined)).toBe(false);
    expect(canManageDebug(null)).toBe(false);
  });

  it('grants exactly the three MANAGE_DEVICES roles', () => {
    const granted = ALL_ROLES.filter((role) => canManageDebug(role));
    expect(granted).toEqual(['PortalAdmin', 'UseCaseAdmin', 'Operator']);
  });

  it('agrees with DEBUG_MANAGE_ROLES for every role in the domain', () => {
    for (const role of ALL_ROLES) {
      expect(canManageDebug(role)).toBe(DEBUG_MANAGE_ROLES.includes(role));
    }
  });
});

describe('canViewDebugLog (VIEW_DEVICE_LOGS)', () => {
  it.each<UserRole>(['PortalAdmin', 'UseCaseAdmin', 'DataScientist', 'Operator', 'Viewer'])(
    'lets %s read the debug log',
    (role) => {
      expect(canViewDebugLog(role)).toBe(true);
    }
  );

  it('does not let a DataLabeler read the debug log', () => {
    expect(canViewDebugLog('DataLabeler')).toBe(false);
  });

  it('denies a missing role (undefined / null)', () => {
    expect(canViewDebugLog(undefined)).toBe(false);
    expect(canViewDebugLog(null)).toBe(false);
  });

  it('grants every role except DataLabeler', () => {
    const granted = ALL_ROLES.filter((role) => canViewDebugLog(role));
    expect(granted).toEqual(['PortalAdmin', 'UseCaseAdmin', 'DataScientist', 'Operator', 'Viewer']);
  });

  it('agrees with DEBUG_VIEW_ROLES for every role in the domain', () => {
    for (const role of ALL_ROLES) {
      expect(canViewDebugLog(role)).toBe(DEBUG_VIEW_ROLES.includes(role));
    }
  });

  it('every role that may manage may also view', () => {
    for (const role of DEBUG_MANAGE_ROLES) {
      expect(canViewDebugLog(role)).toBe(true);
    }
  });
});
