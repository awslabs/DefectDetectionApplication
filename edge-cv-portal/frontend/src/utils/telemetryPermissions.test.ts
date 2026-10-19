/**
 * Unit tests for the workflow telemetry dashboards access predicates
 * (workflow-telemetry-dashboards task 5.1, Requirements 17.1, 19.4).
 *
 * The matrix of design §11.1 over every `UserRole`, read from the shared
 * fixture `telemetryPermissionsMatrix.json` that the backend test
 * `tests/test_telemetry_permissions.py` asserts against the RBAC manager
 * too: the predicates must agree with the fixture row by row, the fixture
 * must hold the six roles with the three permissions, and the derived role
 * lists must be what the design states.
 */

import { describe, expect, it } from 'vitest';
import matrix from './telemetryPermissionsMatrix.json';
import {
  DASHBOARDS_MANAGE_ROLES,
  DASHBOARDS_READ_ROLES,
  TELEMETRY_MANAGE_ROLES,
  TELEMETRY_MATRIX_ROLES,
  TELEMETRY_PERMISSION_MATRIX,
  canManageDashboards,
  canManageTelemetry,
  canReadDashboards,
  hasTelemetryPermission,
  type TelemetryPermission,
} from './telemetryPermissions';
import type { UserRole } from '../types';

const ALL_ROLES: readonly UserRole[] = [
  'PortalAdmin',
  'UseCaseAdmin',
  'DataScientist',
  'Operator',
  'Viewer',
  'DataLabeler',
];

const PERMISSIONS: readonly TelemetryPermission[] = ['dashboards:read', 'dashboards:manage', 'telemetry:manage'];

/** The §11.1 table transcribed by hand, independent of the fixture file. */
const DESIGN_TABLE: Record<UserRole, Record<TelemetryPermission, boolean>> = {
  Viewer: { 'dashboards:read': true, 'dashboards:manage': false, 'telemetry:manage': false },
  Operator: { 'dashboards:read': true, 'dashboards:manage': true, 'telemetry:manage': false },
  DataScientist: { 'dashboards:read': true, 'dashboards:manage': true, 'telemetry:manage': false },
  UseCaseAdmin: { 'dashboards:read': true, 'dashboards:manage': true, 'telemetry:manage': true },
  PortalAdmin: { 'dashboards:read': true, 'dashboards:manage': true, 'telemetry:manage': true },
  DataLabeler: { 'dashboards:read': false, 'dashboards:manage': false, 'telemetry:manage': false },
};

describe('the shared matrix fixture (telemetryPermissionsMatrix.json)', () => {
  it('holds exactly the six roles, each with exactly the three permissions', () => {
    expect(Object.keys(matrix).sort()).toEqual([...ALL_ROLES].sort());
    for (const role of ALL_ROLES) {
      expect(Object.keys((matrix as Record<string, unknown>)[role] as object).sort()).toEqual([...PERMISSIONS].sort());
    }
  });

  it('equals the §11.1 table', () => {
    expect(matrix).toEqual(DESIGN_TABLE);
    expect(TELEMETRY_PERMISSION_MATRIX).toEqual(DESIGN_TABLE);
    expect([...TELEMETRY_MATRIX_ROLES].sort()).toEqual([...ALL_ROLES].sort());
  });
});

describe('the predicates agree with the fixture for every role and permission', () => {
  const predicate: Record<TelemetryPermission, (role: UserRole | undefined | null) => boolean> = {
    'dashboards:read': canReadDashboards,
    'dashboards:manage': canManageDashboards,
    'telemetry:manage': canManageTelemetry,
  };

  it.each(ALL_ROLES)('%s', (role) => {
    for (const permission of PERMISSIONS) {
      const expected = DESIGN_TABLE[role][permission];
      expect(predicate[permission](role)).toBe(expected);
      expect(hasTelemetryPermission(role, permission)).toBe(expected);
    }
  });
});

describe('canReadDashboards (dashboards:read)', () => {
  it.each<UserRole>(['Viewer', 'Operator', 'DataScientist', 'UseCaseAdmin', 'PortalAdmin'])('lets %s read', (role) => {
    expect(canReadDashboards(role)).toBe(true);
  });

  it('does not let a DataLabeler read dashboards', () => {
    expect(canReadDashboards('DataLabeler')).toBe(false);
  });

  it('denies a missing role (undefined / null)', () => {
    expect(canReadDashboards(undefined)).toBe(false);
    expect(canReadDashboards(null)).toBe(false);
  });

  it('DASHBOARDS_READ_ROLES is every role except DataLabeler', () => {
    expect([...DASHBOARDS_READ_ROLES].sort()).toEqual(
      ['Viewer', 'Operator', 'DataScientist', 'UseCaseAdmin', 'PortalAdmin'].sort()
    );
  });
});

describe('canManageDashboards (dashboards:manage)', () => {
  it.each<UserRole>(['Operator', 'DataScientist', 'UseCaseAdmin', 'PortalAdmin'])('lets %s manage', (role) => {
    expect(canManageDashboards(role)).toBe(true);
  });

  it.each<UserRole>(['Viewer', 'DataLabeler'])('does not let %s manage', (role) => {
    expect(canManageDashboards(role)).toBe(false);
  });

  it('denies a missing role (undefined / null)', () => {
    expect(canManageDashboards(undefined)).toBe(false);
    expect(canManageDashboards(null)).toBe(false);
  });

  it('DASHBOARDS_MANAGE_ROLES is Operator, DataScientist, UseCaseAdmin and PortalAdmin', () => {
    expect([...DASHBOARDS_MANAGE_ROLES].sort()).toEqual(
      ['Operator', 'DataScientist', 'UseCaseAdmin', 'PortalAdmin'].sort()
    );
  });

  it('every role that may manage may also read', () => {
    for (const role of DASHBOARDS_MANAGE_ROLES) expect(canReadDashboards(role)).toBe(true);
  });
});

describe('canManageTelemetry (telemetry:manage)', () => {
  it.each<UserRole>(['UseCaseAdmin', 'PortalAdmin'])('lets %s manage telemetry', (role) => {
    expect(canManageTelemetry(role)).toBe(true);
  });

  it.each<UserRole>(['Viewer', 'Operator', 'DataScientist', 'DataLabeler'])('does not let %s manage telemetry', (role) => {
    expect(canManageTelemetry(role)).toBe(false);
  });

  it('denies a missing role (undefined / null)', () => {
    expect(canManageTelemetry(undefined)).toBe(false);
    expect(canManageTelemetry(null)).toBe(false);
  });

  it('TELEMETRY_MANAGE_ROLES is UseCaseAdmin and PortalAdmin', () => {
    expect([...TELEMETRY_MANAGE_ROLES].sort()).toEqual(['UseCaseAdmin', 'PortalAdmin'].sort());
  });

  it('every role that may manage telemetry may also manage dashboards', () => {
    for (const role of TELEMETRY_MANAGE_ROLES) expect(canManageDashboards(role)).toBe(true);
  });
});
