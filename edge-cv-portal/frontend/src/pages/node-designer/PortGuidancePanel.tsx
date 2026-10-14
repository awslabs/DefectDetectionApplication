/**
 * Port guidance panel (port-guidance-and-pad-prepopulation, task 6.3,
 * Requirements 1.1–1.5, 2.1–2.5).
 *
 * One shared component rendered inside the Ports step of both wizards
 * (Requirement 1.5). Fully static: every string comes from the pure
 * data module `portGuidance.ts` and the component issues no network
 * request (Requirement 1.4), so the guidance renders identically with
 * or without a Plugin_Record.
 *
 * - The Port definition, the Workflow_Designer connection rule, the
 *   input/output distinction, and the three Port_Type descriptions
 *   (carries + usage example) live in a Cloudscape ExpandableSection
 *   (Requirements 1.1–1.3).
 * - The selected category's typical arrangement summary re-renders on
 *   the `category` prop (Requirements 2.1, 2.2).
 * - When the declared ports diverge from the category arrangement
 *   (guidanceDivergence non-null), a dismissable non-blocking
 *   `Alert type="info"` names the diverging side(s) (Requirement 2.4)
 *   and disappears when the divergence resolves (Requirement 2.5); a
 *   dismissed alert re-arms once the declaration matches again.
 *
 * The panel is purely advisory: it never contributes to
 * `portsStepErrors` or step gating (Requirement 2.3).
 */
import { useEffect, useState } from 'react';
import {
  Alert,
  Box,
  ExpandableSection,
  SpaceBetween,
} from '@cloudscape-design/components';
import type { PortForm } from './declaration';
import {
  CATEGORY_ARRANGEMENTS,
  CONNECTION_RULE,
  INPUT_OUTPUT_DISTINCTION,
  PORT_DEFINITION,
  PORT_TYPE_GUIDANCE,
  PORT_TYPES,
  arrangementRequirements,
  guidanceDivergence,
} from './portGuidance';
import type { NodeCategory } from './types';

export interface PortGuidancePanelProps {
  /** Selected palette category; drives the arrangement box (2.1, 2.2). */
  category: string;
  /** The wizard's current port rows, for the divergence advisory (2.4). */
  inputs: PortForm[];
  outputs: PortForm[];
}

/** The human-readable side name(s) of a divergence result (2.4). */
function divergingSides(flags: { inputs: boolean; outputs: boolean }): string {
  if (flags.inputs && flags.outputs) {
    return 'inputs and outputs';
  }
  return flags.inputs ? 'inputs' : 'outputs';
}

export default function PortGuidancePanel({
  category,
  inputs,
  outputs,
}: PortGuidancePanelProps) {
  const arrangement = Object.prototype.hasOwnProperty.call(
    CATEGORY_ARRANGEMENTS,
    category
  )
    ? CATEGORY_ARRANGEMENTS[category as NodeCategory]
    : null;

  // Per-kind input/output requirements statement (workflow-designer-
  // bugfixes Bug 2, Requirement 2.6). Purely advisory like the rest of
  // the panel — never contributes to step gating.
  const requirements = arrangementRequirements(category);

  const divergence = guidanceDivergence(category, inputs, outputs);

  // Dismissal hides the advisory until the divergence resolves; once
  // the declaration matches the arrangement again the alert re-arms,
  // so a future divergence is advised anew (2.4, 2.5).
  const [dismissed, setDismissed] = useState(false);
  const diverges = divergence !== null;
  useEffect(() => {
    if (!diverges) {
      setDismissed(false);
    }
  }, [diverges]);

  return (
    <SpaceBetween size="s" data-testid="port-guidance-panel">
      <ExpandableSection
        headerText="What are ports?"
        defaultExpanded
        data-testid="port-guidance-section"
      >
        <SpaceBetween size="xs">
          <Box data-testid="port-guidance-definition">
            {PORT_DEFINITION} {CONNECTION_RULE}
          </Box>
          <Box data-testid="port-guidance-distinction">
            {INPUT_OUTPUT_DISTINCTION}
          </Box>
          {PORT_TYPES.map((portType) => (
            <Box key={portType} data-testid={`port-guidance-type-${portType}`}>
              <Box variant="strong">{portType}</Box>{' '}
              {PORT_TYPE_GUIDANCE[portType].carries}{' '}
              {PORT_TYPE_GUIDANCE[portType].example}
            </Box>
          ))}
        </SpaceBetween>
      </ExpandableSection>

      {arrangement && (
        <Box
          color="text-body-secondary"
          data-testid="port-guidance-arrangement"
        >
          {arrangement.summary}
        </Box>
      )}

      {requirements && (
        <Box data-testid="port-guidance-requirements">
          <Box variant="strong">Typical requirement for this node kind:</Box>{' '}
          {requirements}
        </Box>
      )}

      {divergence && !dismissed && (
        <Alert
          type="info"
          dismissible
          onDismiss={() => setDismissed(true)}
          header="Ports differ from the typical arrangement"
          data-testid="port-guidance-divergence-alert"
        >
          Your declared {divergingSides(divergence)} differ from the typical
          arrangement for the selected category. This is only guidance — any
          valid port declaration is accepted.
        </Alert>
      )}
    </SpaceBetween>
  );
}
