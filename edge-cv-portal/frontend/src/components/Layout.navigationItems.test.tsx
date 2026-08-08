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

/** Texts of the link items, in order (dividers dropped). */
function linkTexts(items: SideNavigationProps.Item[]): string[] {
  return items
    .filter((item): item is SideNavigationProps.Link => item.type === 'link')
    .map((item) => item.text);
}

function hrefFor(
  items: SideNavigationProps.Item[],
  text: string
): string | undefined {
  return items
    .filter((item): item is SideNavigationProps.Link => item.type === 'link')
    .find((item) => item.text === text)?.href;
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
      'Devices',
    ]);
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
