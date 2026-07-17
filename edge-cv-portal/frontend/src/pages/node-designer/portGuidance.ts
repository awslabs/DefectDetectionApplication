/**
 * Static Port_Guidance content and the category-divergence rule
 * (port-guidance-and-pad-prepopulation, Requirements 1, 2).
 *
 * Pure data plus one pure function, no imports beyond `types.ts`, so
 * `PortGuidancePanel` renders identical guidance in both wizards with
 * no network access (Requirements 1.4, 1.5). Port lists are accepted
 * structurally (`{ portType }`), so the wizards' `PortForm` rows
 * (declaration.ts) pass through unchanged without coupling this module
 * to the form-state types.
 */
import { PORT_TYPES, type NodeCategory, type PortType } from './types';

// ----------------------------------------------------------- definitions

/** What a Port is (Requirement 1.1). */
export const PORT_DEFINITION =
  'A port is one declared connection point of your node type. Each port ' +
  'has a name and a port type describing the data that flows through it. ' +
  'In the Workflow Designer, workflows are assembled by connecting node ' +
  'ports on the canvas.';

/** The connection rule the Workflow_Designer enforces (Requirement 1.1). */
export const CONNECTION_RULE =
  'A workflow connection joins an output port of one node to an input ' +
  'port of another node, and both ports must have a compatible port type.';

/** Input vs output ports (Requirement 1.3). */
export const INPUT_OUTPUT_DISTINCTION =
  'Input ports receive data from an upstream node; output ports send the ' +
  'data your node produces to a downstream node.';

/**
 * Per-Port_Type guidance (Requirement 1.2): the data the type carries
 * and a usage example that names a node role and states whether that
 * role uses the type as an input or an output.
 */
export const PORT_TYPE_GUIDANCE: Record<
  PortType,
  { carries: string; example: string }
> = {
  VideoFrames: {
    carries: 'A stream of video frames (raw or decoded video data).',
    example:
      'A camera input node produces VideoFrames as an output; a ' +
      'preprocessing node such as a resize filter receives VideoFrames ' +
      'as an input.',
  },
  InferenceMeta: {
    carries:
      'Inference results such as detections or classifications attached ' +
      'to frames.',
    example:
      'An inference node such as an object detector produces ' +
      'InferenceMeta as an output; a post-processing node receives ' +
      'InferenceMeta as an input.',
  },
  EventSignal: {
    carries: 'Discrete trigger or notification events.',
    example:
      'A post-processing node such as a defect trigger produces ' +
      'EventSignal as an output; an output node such as an alert ' +
      'publisher receives EventSignal as an input.',
  },
};

// ----------------------------------------------- category arrangements

/**
 * Typical arrangement per palette category (Requirement 2.1).
 * `'at-least-one'` models the output category's "at least one input of
 * any type".
 */
export interface CategoryArrangement {
  inputs: PortType[] | 'at-least-one';
  outputs: PortType[];
  /** Human-readable arrangement text. */
  summary: string;
}

export const CATEGORY_ARRANGEMENTS: Record<NodeCategory, CategoryArrangement> =
  {
    input: {
      inputs: [],
      outputs: ['VideoFrames'],
      summary:
        'Input nodes typically declare no inputs and one VideoFrames ' +
        'output: they bring video into the workflow.',
    },
    preprocessing: {
      inputs: ['VideoFrames'],
      outputs: ['VideoFrames'],
      summary:
        'Preprocessing nodes typically declare one VideoFrames input and ' +
        'one VideoFrames output: they transform frames in place.',
    },
    inference: {
      inputs: ['VideoFrames'],
      outputs: ['InferenceMeta'],
      summary:
        'Inference nodes typically declare one VideoFrames input and one ' +
        'InferenceMeta output: they analyze frames and emit results.',
    },
    post_processing: {
      inputs: ['InferenceMeta'],
      outputs: ['EventSignal'],
      summary:
        'Post-processing nodes typically declare one InferenceMeta input ' +
        'and one EventSignal output: they turn inference results into ' +
        'events.',
    },
    output: {
      inputs: 'at-least-one',
      outputs: [],
      summary:
        'Output nodes typically declare at least one input (of any port ' +
        'type) and no outputs: they consume workflow data.',
    },
  };

/** One side of the per-kind requirements line (e.g. "1 × VideoFrames"). */
function sideRequirement(arrangement: PortType[] | 'at-least-one'): string {
  if (arrangement === 'at-least-one') {
    return 'at least one (any port type)';
  }
  if (arrangement.length === 0) {
    return 'none';
  }
  const counts = new Map<PortType, number>();
  for (const portType of arrangement) {
    counts.set(portType, (counts.get(portType) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([portType, count]) => `${count} × ${portType}`)
    .join(', ');
}

/**
 * Explicit per-kind input/output requirements statement for one
 * palette category (workflow-designer-bugfixes Bug 2, Requirement
 * 2.6), derived from CATEGORY_ARRANGEMENTS: e.g. input →
 * "Inputs: none · Outputs: 1 × VideoFrames"; output →
 * "Inputs: at least one (any port type) · Outputs: none". An unknown
 * category has no arrangement, so it answers null. Pure and
 * deterministic; purely advisory — never contributes to step gating.
 */
export function arrangementRequirements(category: string): string | null {
  if (!Object.prototype.hasOwnProperty.call(CATEGORY_ARRANGEMENTS, category)) {
    return null;
  }
  const arrangement = CATEGORY_ARRANGEMENTS[category as NodeCategory];
  return `Inputs: ${sideRequirement(arrangement.inputs)} · Outputs: ${sideRequirement(arrangement.outputs)}`;
}

// ----------------------------------------------------------- divergence

/** The port shape the divergence rule needs; `PortForm` satisfies it. */
interface PortLike {
  portType: string;
}

/**
 * Whether one side of the declaration diverges from its arrangement:
 * `'at-least-one'` diverges only when the side is empty; a concrete
 * arrangement diverges unless the side's port count and multiset of
 * port types match (order-insensitive).
 */
function sideDiverges(
  arrangement: PortType[] | 'at-least-one',
  ports: readonly PortLike[]
): boolean {
  if (arrangement === 'at-least-one') {
    return ports.length === 0;
  }
  if (ports.length !== arrangement.length) {
    return true;
  }
  const remaining = new Map<string, number>();
  for (const portType of arrangement) {
    remaining.set(portType, (remaining.get(portType) ?? 0) + 1);
  }
  for (const port of ports) {
    const count = remaining.get(port.portType) ?? 0;
    if (count === 0) {
      return true;
    }
    remaining.set(port.portType, count - 1);
  }
  // Counts and every decrement matched, so the multisets are equal.
  return false;
}

/**
 * Divergence of a declaration from the selected category's arrangement
 * (Requirements 2.4, 2.5): null when each side's port count and
 * multiset of port types match the arrangement; otherwise flags exactly
 * the diverging side(s). An unknown category has no arrangement to
 * diverge from, so it answers null. Pure and deterministic.
 */
export function guidanceDivergence(
  category: string,
  inputs: readonly PortLike[],
  outputs: readonly PortLike[]
): { inputs: boolean; outputs: boolean } | null {
  if (!Object.prototype.hasOwnProperty.call(CATEGORY_ARRANGEMENTS, category)) {
    return null;
  }
  const arrangement = CATEGORY_ARRANGEMENTS[category as NodeCategory];
  const inputsDiverge = sideDiverges(arrangement.inputs, inputs);
  const outputsDiverge = sideDiverges(arrangement.outputs, outputs);
  if (!inputsDiverge && !outputsDiverge) {
    return null;
  }
  return { inputs: inputsDiverge, outputs: outputsDiverge };
}

// PORT_TYPES is re-exported reading for consumers iterating the guidance
// in catalog order (e.g. the guidance panel).
export { PORT_TYPES };
