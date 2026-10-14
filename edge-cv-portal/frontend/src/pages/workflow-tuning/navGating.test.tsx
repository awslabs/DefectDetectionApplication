/**
 * Unit tests for the Workflow Tuning navigation gating and the section
 * landing page (quality-prompt-tuning, task 8.6 — Requirements 1.1, 1.2).
 *
 * Example-based counterparts of Property 19's first clause: the concrete
 * shape of the navigation entry (an `expandable-link-group` named
 * "Workflow Tuning" with `/workflow-tuning` and one "VLM/LLM Anomaly
 * Tuning" sub-entry), its exact POSITION between "Workflows" and "Node
 * Designer", the three roles it is offered to, that no `/workflow-tuning`
 * href leaks to any other role, and that the pre-existing navigation is
 * left untouched by the insertion. The landing page's tool list is checked
 * here too: it is what the group's own href opens (Requirement 1.2).
 *
 * No AWS and no network: `buildNavigationItems` is pure, and the landing
 * page only needs `useNavigate`.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { SideNavigationProps } from '@cloudscape-design/components';
import { buildNavigationItems } from '../../components/Layout';
import {
  WORKFLOW_TUNING_ACCESS_ROLES,
  canAccessWorkflowTuning,
} from '../../utils/workflowTuningAccess';
import WorkflowTuningLanding, {
  WORKFLOW_TUNING_TOOLS,
} from './WorkflowTuningLanding';
import type { UserRole } from '../../types';

const { navigateMock } = vi.hoisted(() => ({ navigateMock: vi.fn() }));

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return { ...actual, useNavigate: () => navigateMock };
});

const ALL_ROLES: readonly UserRole[] = [
  'PortalAdmin',
  'UseCaseAdmin',
  'DataScientist',
  'Operator',
  'Viewer',
  'DataLabeler',
];

/** The group text, in navigation order, of every group-ish/link item. */
function itemTexts(items: SideNavigationProps.Item[]): string[] {
  return items
    .filter(
      (item): item is Extract<SideNavigationProps.Item, { text: string }> =>
        item.type !== 'divider'
    )
    .map((item) => item.text);
}

/** Every href reachable from the list, sub-entries included. */
function allHrefs(items: readonly unknown[]): string[] {
  const hrefs: string[] = [];
  const walk = (list: readonly unknown[]) => {
    for (const item of list) {
      const record = item as { href?: string; items?: readonly unknown[] };
      if (typeof record.href === 'string') hrefs.push(record.href);
      if (Array.isArray(record.items)) walk(record.items);
    }
  };
  walk(items);
  return hrefs;
}

function tuningGroup(role: UserRole | undefined) {
  return buildNavigationItems(role).find(
    (item) =>
      item.type === 'expandable-link-group' && item.text === 'Workflow Tuning'
  ) as
    | { href?: string; items?: Array<{ type?: string; text?: string; href?: string }> }
    | undefined;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Requirement 1.1: the "Workflow Tuning" navigation entry', () => {
  it('is offered to exactly DataScientist, UseCaseAdmin and PortalAdmin', () => {
    const permitted = ALL_ROLES.filter((role) => tuningGroup(role) !== undefined);
    expect([...permitted].sort()).toEqual(
      [...WORKFLOW_TUNING_ACCESS_ROLES].sort()
    );
    expect([...WORKFLOW_TUNING_ACCESS_ROLES].sort()).toEqual([
      'DataScientist',
      'PortalAdmin',
      'UseCaseAdmin',
    ]);
    // The predicate the layout and the route guards share agrees.
    for (const role of ALL_ROLES) {
      expect(canAccessWorkflowTuning(role)).toBe(tuningGroup(role) !== undefined);
    }
  });

  it('is absent for a role-less (still loading) user', () => {
    expect(tuningGroup(undefined)).toBeUndefined();
    expect(canAccessWorkflowTuning(undefined)).toBe(false);
    expect(canAccessWorkflowTuning(null)).toBe(false);
  });

  it('is an expandable link group at /workflow-tuning with the anomaly sub-entry', () => {
    const group = tuningGroup('DataScientist');
    expect(group?.href).toBe('/workflow-tuning');
    expect(group?.items).toEqual([
      {
        type: 'link',
        text: 'VLM/LLM Anomaly Tuning',
        href: '/workflow-tuning/anomaly',
      },
    ]);
  });

  it('sits between "Workflows" and "Node Designer"', () => {
    for (const role of WORKFLOW_TUNING_ACCESS_ROLES) {
      const texts = itemTexts(buildNavigationItems(role));
      const workflows = texts.indexOf('Workflows');
      const tuning = texts.indexOf('Workflow Tuning');
      const designer = texts.indexOf('Node Designer');
      expect(workflows).toBeGreaterThanOrEqual(0);
      expect(tuning).toBe(workflows + 1);
      expect(designer).toBe(tuning + 1);
    }
  });

  it('leaks no /workflow-tuning href to a role without access', () => {
    for (const role of ['Operator', 'Viewer', 'DataLabeler'] as UserRole[]) {
      expect(
        allHrefs(buildNavigationItems(role)).filter((href) =>
          href.startsWith('/workflow-tuning')
        )
      ).toEqual([]);
    }
    expect(
      allHrefs(buildNavigationItems(undefined)).filter((href) =>
        href.startsWith('/workflow-tuning')
      )
    ).toEqual([]);
  });

  it('adds the group without changing the rest of the navigation', () => {
    const withTuning = itemTexts(buildNavigationItems('DataScientist'));
    const withoutTuning = itemTexts(buildNavigationItems('Operator'));
    // The DataScientist keeps the gated entries Operator does not get, so
    // compare only the removal of "Workflow Tuning" from the shared spine.
    expect(withTuning.filter((text) => text !== 'Workflow Tuning')).toEqual(
      expect.arrayContaining(
        withoutTuning.filter((text) => text !== 'Synthetic Data')
      )
    );
    expect(withTuning.filter((text) => text === 'Workflow Tuning')).toHaveLength(1);
  });
});

describe('Requirement 1.2: the section landing page lists its tools', () => {
  it('lists VLM/LLM Anomaly Tuning as the section tool', () => {
    expect(WORKFLOW_TUNING_TOOLS.map((tool) => tool.id)).toEqual(['anomaly']);
    expect(WORKFLOW_TUNING_TOOLS[0].name).toBe('VLM/LLM Anomaly Tuning');
    expect(WORKFLOW_TUNING_TOOLS[0].href).toBe('/workflow-tuning/anomaly');
    expect(WORKFLOW_TUNING_TOOLS[0].description).not.toBe('');
  });

  it('renders every tool with a description and opens it on the action', () => {
    render(<WorkflowTuningLanding />);
    for (const tool of WORKFLOW_TUNING_TOOLS) {
      expect(screen.getByText(tool.name)).toBeTruthy();
      expect(screen.getByText(tool.description)).toBeTruthy();
      fireEvent.click(screen.getByRole('button', { name: `Open ${tool.name}` }));
      expect(navigateMock).toHaveBeenLastCalledWith(tool.href);
    }
  });
});
