/**
 * Component tests for PortGuidancePanel
 * (port-guidance-and-pad-prepopulation task 6.4, Requirements 1.1,
 * 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 2.5).
 *
 * Panel-level behavior against the static `portGuidance.ts` data:
 * the guidance content (Port definition + connection rule, the
 * input/output distinction, all three Port_Types with carries +
 * example; 1.1–1.3), the five category arrangements displayed and
 * swapping on the `category` prop (2.1, 2.2), the divergence advisory
 * appearing/disappearing/dismissing without ever blocking (2.3–2.5),
 * and the absence of network calls (1.4).
 *
 * Wizard-level wiring (panel present in both wizards, step gating) is
 * covered in RegistrationWizard.test.tsx and CreateWizard.test.tsx.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import PortGuidancePanel from './PortGuidancePanel';
import type { PortForm } from './declaration';
import {
  CATEGORY_ARRANGEMENTS,
  CONNECTION_RULE,
  INPUT_OUTPUT_DISTINCTION,
  PORT_DEFINITION,
  PORT_TYPE_GUIDANCE,
} from './portGuidance';
import { CATEGORIES, PORT_TYPES } from './types';

// ------------------------------------------------------------- fixtures

const port = (name: string, portType: string): PortForm => ({
  name,
  portType,
});

/** Ports matching the preprocessing arrangement (1 VideoFrames each side). */
const MATCHING_INPUTS = [port('in', 'VideoFrames')];
const MATCHING_OUTPUTS = [port('out', 'VideoFrames')];

/** An output side diverging from preprocessing (InferenceMeta ≠ VideoFrames). */
const DIVERGING_OUTPUTS = [port('out', 'InferenceMeta')];

const renderPanel = (
  category = 'preprocessing',
  inputs: PortForm[] = MATCHING_INPUTS,
  outputs: PortForm[] = MATCHING_OUTPUTS
) =>
  render(
    <PortGuidancePanel category={category} inputs={inputs} outputs={outputs} />
  );

const divergenceAlert = () =>
  screen.queryByTestId('port-guidance-divergence-alert');

// The panel is fully static (1.4): stub out fetch to prove nothing on
// the network is touched by any render or interaction in this file.
const fetchSpy = vi.fn();

beforeEach(() => {
  vi.stubGlobal('fetch', fetchSpy);
});

afterEach(() => {
  expect(fetchSpy).not.toHaveBeenCalled();
  vi.unstubAllGlobals();
});

describe('PortGuidancePanel', () => {
  it('renders the port definition, connection rule, and input/output distinction (1.1, 1.3)', () => {
    renderPanel();

    expect(screen.getByTestId('port-guidance-definition').textContent).toBe(
      `${PORT_DEFINITION} ${CONNECTION_RULE}`
    );
    expect(screen.getByTestId('port-guidance-distinction').textContent).toBe(
      INPUT_OUTPUT_DISTINCTION
    );
  });

  it('describes all three Port_Types with carries and a usage example (1.2)', () => {
    renderPanel();

    expect(PORT_TYPES).toHaveLength(3);
    for (const portType of PORT_TYPES) {
      const box = screen.getByTestId(`port-guidance-type-${portType}`);
      expect(box.textContent).toContain(portType);
      expect(box.textContent).toContain(PORT_TYPE_GUIDANCE[portType].carries);
      expect(box.textContent).toContain(PORT_TYPE_GUIDANCE[portType].example);
    }
  });

  it('defines and displays an arrangement for every palette category (2.1)', () => {
    expect(CATEGORIES).toHaveLength(5);
    for (const category of CATEGORIES) {
      const arrangement = CATEGORY_ARRANGEMENTS[category];
      expect(arrangement.summary).toBeTruthy();

      const { unmount } = renderPanel(category, [], []);
      expect(
        screen.getByTestId('port-guidance-arrangement').textContent
      ).toBe(arrangement.summary);
      unmount();
    }
  });

  it('swaps the arrangement summary when the category prop changes (2.2)', () => {
    const { rerender } = renderPanel('input', [], []);
    expect(screen.getByTestId('port-guidance-arrangement').textContent).toBe(
      CATEGORY_ARRANGEMENTS.input.summary
    );

    rerender(
      <PortGuidancePanel category="inference" inputs={[]} outputs={[]} />
    );
    expect(screen.getByTestId('port-guidance-arrangement').textContent).toBe(
      CATEGORY_ARRANGEMENTS.inference.summary
    );
  });

  it('shows the advisory naming the diverging side when the declaration diverges (2.4)', () => {
    renderPanel('preprocessing', MATCHING_INPUTS, DIVERGING_OUTPUTS);

    const alert = divergenceAlert();
    expect(alert).not.toBeNull();
    expect(alert!.textContent).toContain(
      'Ports differ from the typical arrangement'
    );
    expect(alert!.textContent).toContain('Your declared outputs differ');
    expect(alert!.textContent).toContain('This is only guidance');
  });

  it('names both sides when inputs and outputs diverge (2.4)', () => {
    renderPanel('preprocessing', [], []);

    expect(divergenceAlert()!.textContent).toContain(
      'Your declared inputs and outputs differ'
    );
  });

  it('shows no advisory when the declaration matches the arrangement (2.4)', () => {
    renderPanel('preprocessing', MATCHING_INPUTS, MATCHING_OUTPUTS);

    expect(divergenceAlert()).toBeNull();
  });

  it('never blocks: guidance and arrangement stay rendered alongside the dismissable advisory (2.3)', () => {
    renderPanel('preprocessing', MATCHING_INPUTS, DIVERGING_OUTPUTS);

    // Advisory present, yet every other part of the panel is intact —
    // the advisory only informs and can be dismissed at will.
    expect(divergenceAlert()).not.toBeNull();
    expect(screen.getByTestId('port-guidance-definition')).toBeInTheDocument();
    expect(
      screen.getByTestId('port-guidance-arrangement')
    ).toBeInTheDocument();

    fireEvent.click(within(divergenceAlert()!).getByRole('button'));
    expect(divergenceAlert()).toBeNull();
  });

  it('drops the advisory when the divergence resolves (2.5)', () => {
    const { rerender } = renderPanel(
      'preprocessing',
      MATCHING_INPUTS,
      DIVERGING_OUTPUTS
    );
    expect(divergenceAlert()).not.toBeNull();

    rerender(
      <PortGuidancePanel
        category="preprocessing"
        inputs={MATCHING_INPUTS}
        outputs={MATCHING_OUTPUTS}
      />
    );
    expect(divergenceAlert()).toBeNull();
  });

  it('re-arms a dismissed advisory once the declaration matches again (2.4, 2.5)', () => {
    const { rerender } = renderPanel(
      'preprocessing',
      MATCHING_INPUTS,
      DIVERGING_OUTPUTS
    );

    // Dismiss while diverging: hidden even though the divergence remains.
    fireEvent.click(within(divergenceAlert()!).getByRole('button'));
    expect(divergenceAlert()).toBeNull();

    // Resolve, then diverge anew: the advisory is advised again.
    rerender(
      <PortGuidancePanel
        category="preprocessing"
        inputs={MATCHING_INPUTS}
        outputs={MATCHING_OUTPUTS}
      />
    );
    expect(divergenceAlert()).toBeNull();
    rerender(
      <PortGuidancePanel
        category="preprocessing"
        inputs={MATCHING_INPUTS}
        outputs={DIVERGING_OUTPUTS}
      />
    );
    expect(divergenceAlert()).not.toBeNull();
  });
});
