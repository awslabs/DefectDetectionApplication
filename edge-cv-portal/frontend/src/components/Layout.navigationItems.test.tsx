/**
 * Unit tests for the pure `buildNavigationItems(role)` side-navigation builder
 * (build-fleet-rbac-visibility bugfix, design Part B).
 *
 * Example-based spot checks of the two ends of the role range: a Viewer sees
 * neither the gated "Builds" entry nor the PortalAdmin-only admin group, while
 * a PortalAdmin keeps "Builds", the admin group (including "Build Fleet"), and
 * the audit-logs entry.
 *
 * Validates: Requirements 2.5, 2.6, 3.6
 */

import { describe, expect, it } from 'vitest';
import type { SideNavigationProps } from '@cloudscape-design/components';
import { buildNavigationItems } from './Layout';
import type { UserRole } from '../types';

/** Top-level entries with their own href: plain links and expandable link
 *  groups (Node Designer became a group when Git connections landed). */
type TopLevelEntry = SideNavigationProps.Link | SideNavigationProps.ExpandableLinkGroup;

function topLevelEntries(items: SideNavigationProps.Item[]): TopLevelEntry[] {
  return items.filter(
    (item): item is TopLevelEntry =>
      item.type === 'link' || item.type === 'expandable-link-group'
  );
}

/** Texts of the top-level entries, in order (dividers dropped). */
function linkTexts(items: SideNavigationProps.Item[]): string[] {
  return topLevelEntries(items).map((item) => item.text);
}

function hrefFor(
  items: SideNavigationProps.Item[],
  text: string
): string | undefined {
  return topLevelEntries(items).find((item) => item.text === text)?.href;
}

const ADMIN_GROUP_ITEMS = ['Plugin Review', 'Build Fleet', 'Settings'];

describe('buildNavigationItems — Viewer', () => {
  const items = buildNavigationItems('Viewer');

  it('omits the "Builds" entry', () => {
    expect(linkTexts(items)).not.toContain('Builds');
  });

  it('omits the PortalAdmin-only admin group', () => {
    for (const text of ADMIN_GROUP_ITEMS) {
      expect(linkTexts(items)).not.toContain(text);
    }
  });

  it('omits the audit-logs entry', () => {
    expect(linkTexts(items)).not.toContain('Audit Logs');
  });

  it('keeps the shared navigation entries in order', () => {
    expect(linkTexts(items)).toEqual([
      'Dashboard',
      'Use Cases',
      'Data Management',
      'Labeling',
      'Training',
      'Models',
      'Workflows',
      'Node Designer',
      'Components',
      'Deployments',
      'Workflow dashboards',
      'Devices',
      'Debug log',
    ]);
  });
});

describe('buildNavigationItems — the "Workflow dashboards" entry (workflow-telemetry-dashboards design §11.4)', () => {
  it.each<UserRole>(['Viewer', 'Operator', 'DataScientist', 'UseCaseAdmin', 'PortalAdmin'])(
    'is present for %s, pointing at /workflow-dashboards, right after Deployments',
    (role) => {
      const items = buildNavigationItems(role);
      const texts = linkTexts(items);
      expect(texts).toContain('Workflow dashboards');
      expect(hrefFor(items, 'Workflow dashboards')).toBe('/workflow-dashboards');
      expect(texts.indexOf('Workflow dashboards')).toBe(texts.indexOf('Deployments') + 1);
    }
  );

  it('is absent for DataLabeler, whose navigation is the labeler workspace only', () => {
    expect(linkTexts(buildNavigationItems('DataLabeler'))).toEqual(['My Labeling Tasks']);
  });

  it('is absent for a missing role (loading state)', () => {
    expect(linkTexts(buildNavigationItems(undefined))).not.toContain('Workflow dashboards');
  });

  it('keeps "Devices" and "Debug log" right after it', () => {
    const texts = linkTexts(buildNavigationItems('Operator'));
    expect(texts.indexOf('Devices')).toBe(texts.indexOf('Workflow dashboards') + 1);
    expect(texts.indexOf('Debug log')).toBe(texts.indexOf('Devices') + 1);
  });
});

describe('buildNavigationItems — the "Debug log" entry (workflow-debug-mode Req 2.6, 3.1)', () => {
  it.each<UserRole>(['Viewer', 'Operator', 'DataScientist', 'UseCaseAdmin', 'PortalAdmin'])(
    'is present for %s, pointing at /debug, right after Devices',
    (role) => {
      const items = buildNavigationItems(role);
      const texts = linkTexts(items);
      expect(texts).toContain('Debug log');
      expect(hrefFor(items, 'Debug log')).toBe('/debug');
      expect(texts.indexOf('Debug log')).toBe(texts.indexOf('Devices') + 1);
    }
  );

  it('is absent for DataLabeler, whose navigation is the labeler workspace only', () => {
    expect(linkTexts(buildNavigationItems('DataLabeler'))).toEqual(['My Labeling Tasks']);
  });

  it('is absent for a missing role (loading state)', () => {
    expect(linkTexts(buildNavigationItems(undefined))).not.toContain('Debug log');
  });
});

describe('buildNavigationItems — PortalAdmin', () => {
  const items = buildNavigationItems('PortalAdmin');

  it('includes the "Builds" entry pointing at /builds', () => {
    expect(linkTexts(items)).toContain('Builds');
    expect(hrefFor(items, 'Builds')).toBe('/builds');
  });

  it('includes the admin group with "Build Fleet" → /admin/fleet', () => {
    for (const text of ADMIN_GROUP_ITEMS) {
      expect(linkTexts(items)).toContain(text);
    }
    expect(hrefFor(items, 'Build Fleet')).toBe('/admin/fleet');
  });

  it('includes the audit-logs entry last', () => {
    expect(hrefFor(items, 'Audit Logs')).toBe('/audit');
    expect(linkTexts(items).at(-1)).toBe('Audit Logs');
  });

  it('keeps "Builds" between "Components" and "Deployments"', () => {
    const texts = linkTexts(items);
    expect(texts.indexOf('Builds')).toBe(texts.indexOf('Components') + 1);
    expect(texts.indexOf('Deployments')).toBe(texts.indexOf('Builds') + 1);
  });
});

describe('buildNavigationItems — other roles', () => {
  it.each<UserRole>(['DataScientist', 'UseCaseAdmin'])(
    'shows "Builds" but not "Build Fleet" for %s',
    (role) => {
      const texts = linkTexts(buildNavigationItems(role));
      expect(texts).toContain('Builds');
      expect(texts).not.toContain('Build Fleet');
    }
  );

  it('shows the audit-logs entry for UseCaseAdmin only among non-admins', () => {
    expect(linkTexts(buildNavigationItems('UseCaseAdmin'))).toContain(
      'Audit Logs'
    );
    expect(linkTexts(buildNavigationItems('DataScientist'))).not.toContain(
      'Audit Logs'
    );
  });

  it('treats a missing role (loading state) like a role without builds access', () => {
    const texts = linkTexts(buildNavigationItems(undefined));
    expect(texts).not.toContain('Builds');
    expect(texts).not.toContain('Build Fleet');
    expect(texts).not.toContain('Audit Logs');
  });
});
